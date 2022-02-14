import torch

import utils
from utils.hparams import hparams
from .diff.net import DiffNet
from .diff.shallow_diffusion_tts import GaussianDiffusion, OfflineGaussianDiffusion
from .diffspeech_task import DiffSpeechTask
from vocoders.base_vocoder import get_vocoder_cls, BaseVocoder
from modules.fastspeech.pe import PitchExtractor
from modules.fastspeech.fs2 import FastSpeech2
from modules.diffsinger_midi.fs2 import FastSpeech2MIDI

from usr.diff.candidate_decoder import FFT
from utils.pitch_utils import denorm_f0
from tasks.tts.fs2_utils import FastSpeechDataset
from tasks.tts.fs2 import FastSpeech2Task

import numpy as np
import os
import torch.nn.functional as F

DIFF_DECODERS = {
    'wavenet': lambda hp: DiffNet(hp['audio_num_mel_bins']),
    'fft': lambda hp: FFT(
        hp['hidden_size'], hp['dec_layers'], hp['dec_ffn_kernel_size'], hp['num_heads']),
}


class DiffSingerTask(DiffSpeechTask):
    def __init__(self):
        super(DiffSingerTask, self).__init__()
        self.dataset_cls = FastSpeechDataset
        self.vocoder: BaseVocoder = get_vocoder_cls(hparams)()
        if hparams.get('pe_enable') is not None and hparams['pe_enable']:
            self.pe = PitchExtractor().cuda()
            utils.load_ckpt(self.pe, hparams['pe_ckpt'], 'model', strict=True)
            self.pe.eval()

    def build_tts_model(self):
        mel_bins = hparams['audio_num_mel_bins']
        self.model = GaussianDiffusion(
            phone_encoder=self.phone_encoder,
            out_dims=mel_bins, denoise_fn=DIFF_DECODERS[hparams['diff_decoder_type']](hparams),
            timesteps=hparams['timesteps'],
            K_step=hparams['K_step'],
            loss_type=hparams['diff_loss_type'],
            spec_min=hparams['spec_min'], spec_max=hparams['spec_max'],
        )
        if hparams['fs2_ckpt'] != '':
            utils.load_ckpt(self.model.fs2, hparams['fs2_ckpt'], 'model', strict=True)
            # self.model.fs2.decoder = None
            for k, v in self.model.fs2.named_parameters():
                v.requires_grad = False

    def validation_step(self, sample, batch_idx):
        outputs = {}
        txt_tokens = sample['txt_tokens']  # [B, T_t]

        target = sample['mels']  # [B, T_s, 80]
        energy = sample['energy']
        # fs2_mel = sample['fs2_mels']
        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
        mel2ph = sample['mel2ph']
        f0 = sample['f0']
        uv = sample['uv']

        outputs['losses'] = {}

        outputs['losses'], model_out = self.run_model(self.model, sample, return_output=True, infer=False)


        outputs['total_loss'] = sum(outputs['losses'].values())
        outputs['nsamples'] = sample['nsamples']
        outputs = utils.tensors_to_scalars(outputs)
        if batch_idx < hparams['num_valid_plots']:
            model_out = self.model(
                txt_tokens, spk_embed=spk_embed, mel2ph=mel2ph, f0=f0, uv=uv, energy=energy, ref_mels=None, infer=True)

            if hparams.get('pe_enable') is not None and hparams['pe_enable']:
                gt_f0 = self.pe(sample['mels'])['f0_denorm_pred']  # pe predict from GT mel
                pred_f0 = self.pe(model_out['mel_out'])['f0_denorm_pred']  # pe predict from Pred mel
            else:
                gt_f0 = denorm_f0(sample['f0'], sample['uv'], hparams)
                pred_f0 = model_out.get('f0_denorm')
            self.plot_wav(batch_idx, sample['mels'], model_out['mel_out'], is_mel=True, gt_f0=gt_f0, f0=pred_f0)
            self.plot_mel(batch_idx, sample['mels'], model_out['mel_out'], name=f'diffmel_{batch_idx}')
            self.plot_mel(batch_idx, sample['mels'], model_out['fs2_mel'], name=f'fs2mel_{batch_idx}')
        return outputs


class ShallowDiffusionOfflineDataset(FastSpeechDataset):
    def __getitem__(self, index):
        sample = super(ShallowDiffusionOfflineDataset, self).__getitem__(index)
        item = self._get_item(index)

        if self.prefix != 'train' and hparams['fs2_ckpt'] != '':
            fs2_ckpt = os.path.dirname(hparams['fs2_ckpt'])
            item_name = item['item_name']
            fs2_mel = torch.Tensor(np.load(f'{fs2_ckpt}/P_mels_npy/{item_name}.npy'))  # ~M generated by FFT-singer.
            sample['fs2_mel'] = fs2_mel
        return sample

    def collater(self, samples):
        batch = super(ShallowDiffusionOfflineDataset, self).collater(samples)
        if self.prefix != 'train' and hparams['fs2_ckpt'] != '':
            batch['fs2_mels'] = utils.collate_2d([s['fs2_mel'] for s in samples], 0.0)
        return batch


class DiffSingerOfflineTask(DiffSingerTask):
    def __init__(self):
        super(DiffSingerOfflineTask, self).__init__()
        self.dataset_cls = ShallowDiffusionOfflineDataset

    def build_tts_model(self):
        mel_bins = hparams['audio_num_mel_bins']
        self.model = OfflineGaussianDiffusion(
            phone_encoder=self.phone_encoder,
            out_dims=mel_bins, denoise_fn=DIFF_DECODERS[hparams['diff_decoder_type']](hparams),
            timesteps=hparams['timesteps'],
            K_step=hparams['K_step'],
            loss_type=hparams['diff_loss_type'],
            spec_min=hparams['spec_min'], spec_max=hparams['spec_max'],
        )
        # if hparams['fs2_ckpt'] != '':
        #     utils.load_ckpt(self.model.fs2, hparams['fs2_ckpt'], 'model', strict=True)
        #     self.model.fs2.decoder = None

    def run_model(self, model, sample, return_output=False, infer=False):
        txt_tokens = sample['txt_tokens']  # [B, T_t]
        target = sample['mels']  # [B, T_s, 80]
        mel2ph = sample['mel2ph']  # [B, T_s]
        f0 = sample['f0']
        uv = sample['uv']
        energy = sample['energy']
        fs2_mel = None #sample['fs2_mels']
        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
        if hparams['pitch_type'] == 'cwt':
            cwt_spec = sample[f'cwt_spec']
            f0_mean = sample['f0_mean']
            f0_std = sample['f0_std']
            sample['f0_cwt'] = f0 = model.cwt2f0_norm(cwt_spec, f0_mean, f0_std, mel2ph)

        output = model(txt_tokens, mel2ph=mel2ph, spk_embed=spk_embed,
                       ref_mels=[target, fs2_mel], f0=f0, uv=uv, energy=energy, infer=infer)

        losses = {}
        if 'diff_loss' in output:
            losses['mel'] = output['diff_loss']
        # self.add_dur_loss(output['dur'], mel2ph, txt_tokens, losses=losses)
        # if hparams['use_pitch_embed']:
        #     self.add_pitch_loss(output, sample, losses)
        if hparams['use_energy_embed']:
            self.add_energy_loss(output['energy_pred'], energy, losses)

        if not return_output:
            return losses
        else:
            return losses, output

    def validation_step(self, sample, batch_idx):
        outputs = {}
        txt_tokens = sample['txt_tokens']  # [B, T_t]

        target = sample['mels']  # [B, T_s, 80]
        energy = sample['energy']
        # fs2_mel = sample['fs2_mels']
        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
        mel2ph = sample['mel2ph']
        f0 = sample['f0']
        uv = sample['uv']

        outputs['losses'] = {}

        outputs['losses'], model_out = self.run_model(self.model, sample, return_output=True, infer=False)


        outputs['total_loss'] = sum(outputs['losses'].values())
        outputs['nsamples'] = sample['nsamples']
        outputs = utils.tensors_to_scalars(outputs)
        if batch_idx < hparams['num_valid_plots']:
            fs2_mel = sample['fs2_mels']
            model_out = self.model(
                txt_tokens, spk_embed=spk_embed, mel2ph=mel2ph, f0=f0, uv=uv, energy=energy,
                ref_mels=[None, fs2_mel], infer=True)
            if hparams.get('pe_enable') is not None and hparams['pe_enable']:
                gt_f0 = self.pe(sample['mels'])['f0_denorm_pred']  # pe predict from GT mel
                pred_f0 = self.pe(model_out['mel_out'])['f0_denorm_pred']  # pe predict from Pred mel
            else:
                gt_f0 = denorm_f0(sample['f0'], sample['uv'], hparams)
                pred_f0 = model_out.get('f0_denorm')
            self.plot_wav(batch_idx, sample['mels'], model_out['mel_out'], is_mel=True, gt_f0=gt_f0, f0=pred_f0)
            self.plot_mel(batch_idx, sample['mels'], model_out['mel_out'], name=f'diffmel_{batch_idx}')
            self.plot_mel(batch_idx, sample['mels'], fs2_mel, name=f'fs2mel_{batch_idx}')
        return outputs

    def test_step(self, sample, batch_idx):
        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
        txt_tokens = sample['txt_tokens']
        energy = sample['energy']
        if hparams['profile_infer']:
            pass
        else:
            mel2ph, uv, f0 = None, None, None
            if hparams['use_gt_dur']:
                mel2ph = sample['mel2ph']
            if hparams['use_gt_f0']:
                f0 = sample['f0']
                uv = sample['uv']
            fs2_mel = sample['fs2_mels']
            outputs = self.model(
                txt_tokens, spk_embed=spk_embed, mel2ph=mel2ph, f0=f0, uv=uv, ref_mels=[None, fs2_mel], energy=energy,
                infer=True)
            sample['outputs'] = self.model.out2mel(outputs['mel_out'])
            sample['mel2ph_pred'] = outputs['mel2ph']

            if hparams.get('pe_enable') is not None and hparams['pe_enable']:
                sample['f0'] = self.pe(sample['mels'])['f0_denorm_pred']  # pe predict from GT mel
                sample['f0_pred'] = self.pe(sample['outputs'])['f0_denorm_pred']  # pe predict from Pred mel
            else:
                sample['f0'] = denorm_f0(sample['f0'], sample['uv'], hparams)
                sample['f0_pred'] = outputs.get('f0_denorm')
            return self.after_infer(sample)


class MIDIDataset(FastSpeechDataset):
    def __getitem__(self, index):
        sample = super(MIDIDataset, self).__getitem__(index)
        item = self._get_item(index)
        sample['f0_midi'] = torch.FloatTensor(item['f0_midi'])
        sample['pitch_midi'] = torch.LongTensor(item['pitch_midi'])[:hparams['max_frames']]

        return sample

    def collater(self, samples):
        batch = super(MIDIDataset, self).collater(samples)
        batch['f0_midi'] = utils.collate_1d([s['f0_midi'] for s in samples], 0.0)
        batch['pitch_midi'] = utils.collate_1d([s['pitch_midi'] for s in samples], 0)
        # print((batch['pitch_midi'] == f0_to_coarse(batch['f0_midi'])).all())
        return batch


class OpencpopDataset(FastSpeechDataset):
    def __getitem__(self, index):
        sample = super(OpencpopDataset, self).__getitem__(index)
        item = self._get_item(index)
        sample['pitch_midi'] = torch.LongTensor(item['pitch_midi'])[:hparams['max_frames']]
        sample['midi_dur'] = torch.FloatTensor(item['midi_dur'])[:hparams['max_frames']]
        sample['is_slur'] = torch.LongTensor(item['is_slur'])[:hparams['max_frames']]
        return sample

    def collater(self, samples):
        batch = super(OpencpopDataset, self).collater(samples)
        batch['pitch_midi'] = utils.collate_1d([s['pitch_midi'] for s in samples], 0)
        batch['midi_dur'] = utils.collate_1d([s['midi_dur'] for s in samples], 0)
        batch['is_slur'] = utils.collate_1d([s['is_slur'] for s in samples], 0)
        return batch


class DiffSingerMIDITask(DiffSingerTask):
    def __init__(self):
        super(DiffSingerMIDITask, self).__init__()
        # self.dataset_cls = MIDIDataset
        self.dataset_cls = OpencpopDataset

    def run_model(self, model, sample, return_output=False, infer=False):
        txt_tokens = sample['txt_tokens']  # [B, T_t]
        target = sample['mels']  # [B, T_s, 80]
        # mel2ph = sample['mel2ph'] if hparams['use_gt_dur'] else None # [B, T_s]
        mel2ph = sample['mel2ph']
        if hparams.get('switch_midi2f0_step') is not None and self.global_step > hparams['switch_midi2f0_step']:
            f0 = None
            uv = None
        else:
            f0 = sample['f0']
            uv = sample['uv']
        energy = sample['energy']

        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
        if hparams['pitch_type'] == 'cwt':
            cwt_spec = sample[f'cwt_spec']
            f0_mean = sample['f0_mean']
            f0_std = sample['f0_std']
            sample['f0_cwt'] = f0 = model.cwt2f0_norm(cwt_spec, f0_mean, f0_std, mel2ph)

        output = model(txt_tokens, mel2ph=mel2ph, spk_embed=spk_embed,
                       ref_mels=target, f0=f0, uv=uv, energy=energy, infer=infer, pitch_midi=sample['pitch_midi'],
                       midi_dur=sample.get('midi_dur'), is_slur=sample.get('is_slur'))

        losses = {}
        if 'diff_loss' in output:
            losses['mel'] = output['diff_loss']
        if hparams['use_pitch_embed']:
            self.add_pitch_loss(output, sample, losses)
        if hparams['use_energy_embed']:
            self.add_energy_loss(output['energy_pred'], energy, losses)
        if not return_output:
            return losses
        else:
            return losses, output

    def validation_step(self, sample, batch_idx):
        outputs = {}
        txt_tokens = sample['txt_tokens']  # [B, T_t]

        target = sample['mels']  # [B, T_s, 80]
        energy = sample['energy']
        # fs2_mel = sample['fs2_mels']
        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
        mel2ph = sample['mel2ph']

        outputs['losses'] = {}

        outputs['losses'], model_out = self.run_model(self.model, sample, return_output=True, infer=False)

        outputs['total_loss'] = sum(outputs['losses'].values())
        outputs['nsamples'] = sample['nsamples']
        outputs = utils.tensors_to_scalars(outputs)
        if batch_idx < hparams['num_valid_plots']:
            model_out = self.model(
                txt_tokens, spk_embed=spk_embed, mel2ph=mel2ph, f0=None, uv=None, energy=energy, ref_mels=None, infer=True,
                pitch_midi=sample['pitch_midi'], midi_dur=sample.get('midi_dur'), is_slur=sample.get('is_slur'))

            if hparams.get('pe_enable') is not None and hparams['pe_enable']:
                gt_f0 = self.pe(sample['mels'])['f0_denorm_pred']  # pe predict from GT mel
                pred_f0 = self.pe(model_out['mel_out'])['f0_denorm_pred']  # pe predict from Pred mel
            else:
                gt_f0 = denorm_f0(sample['f0'], sample['uv'], hparams)
                pred_f0 = model_out.get('f0_denorm')
            self.plot_wav(batch_idx, sample['mels'], model_out['mel_out'], is_mel=True, gt_f0=gt_f0, f0=pred_f0)
            self.plot_mel(batch_idx, sample['mels'], model_out['mel_out'], name=f'diffmel_{batch_idx}')
            self.plot_mel(batch_idx, sample['mels'], model_out['fs2_mel'], name=f'fs2mel_{batch_idx}')
            if hparams['use_pitch_embed']:
                self.plot_pitch(batch_idx, sample, model_out)
        return outputs


class AuxDecoderMIDITask(FastSpeech2Task):
    def __init__(self):
        super().__init__()
        # self.dataset_cls = MIDIDataset
        self.dataset_cls = OpencpopDataset

    def build_tts_model(self):
        if hparams.get('use_midi') is not None and hparams['use_midi']:
            self.model = FastSpeech2MIDI(self.phone_encoder)
        else:
            self.model = FastSpeech2(self.phone_encoder)

    def run_model(self, model, sample, return_output=False):
        txt_tokens = sample['txt_tokens']  # [B, T_t]
        target = sample['mels']  # [B, T_s, 80]
        mel2ph = sample['mel2ph']  # [B, T_s]
        f0 = sample['f0']
        uv = sample['uv']
        energy = sample['energy']

        spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
        if hparams['pitch_type'] == 'cwt':
            cwt_spec = sample[f'cwt_spec']
            f0_mean = sample['f0_mean']
            f0_std = sample['f0_std']
            sample['f0_cwt'] = f0 = model.cwt2f0_norm(cwt_spec, f0_mean, f0_std, mel2ph)

        output = model(txt_tokens, mel2ph=mel2ph, spk_embed=spk_embed,
                       ref_mels=target, f0=f0, uv=uv, energy=energy, infer=False, pitch_midi=sample['pitch_midi'],
                       midi_dur=sample.get('midi_dur'), is_slur=sample.get('is_slur'))

        losses = {}
        self.add_mel_loss(output['mel_out'], target, losses)
        # self.add_dur_loss(output['dur'], mel2ph, txt_tokens, losses=losses)
        if hparams['use_pitch_embed']:
            self.add_pitch_loss(output, sample, losses)
        if hparams['use_energy_embed']:
            self.add_energy_loss(output['energy_pred'], energy, losses)
        if not return_output:
            return losses
        else:
            return losses, output

    def validation_step(self, sample, batch_idx):
        outputs = {}
        outputs['losses'] = {}
        outputs['losses'], model_out = self.run_model(self.model, sample, return_output=True)
        outputs['total_loss'] = sum(outputs['losses'].values())
        outputs['nsamples'] = sample['nsamples']
        mel_out = self.model.out2mel(model_out['mel_out'])
        outputs = utils.tensors_to_scalars(outputs)
        # if sample['mels'].shape[0] == 1:
        #     self.add_laplace_var(mel_out, sample['mels'], outputs)
        if batch_idx < hparams['num_valid_plots']:
            self.plot_mel(batch_idx, sample['mels'], mel_out)
            self.plot_dur(batch_idx, sample, model_out)
            if hparams['use_pitch_embed']:
                self.plot_pitch(batch_idx, sample, model_out)
        return outputs


# class MIDI2PitchTask(DiffSingerTask):
#     def __init__(self):
#         super(DiffSpeechTask, self).__init__()
#         self.dataset_cls = MIDIDataset
#
#     def build_tts_model(self):
#         mel_bins = hparams['audio_num_mel_bins']
#         self.model = GaussianDiffusion(
#             phone_encoder=self.phone_encoder,
#             out_dims=mel_bins, denoise_fn=DIFF_DECODERS[hparams['diff_decoder_type']](hparams),
#             timesteps=hparams['timesteps'],
#             K_step=hparams['K_step'],
#             loss_type=hparams['diff_loss_type'],
#             spec_min=hparams['spec_min'], spec_max=hparams['spec_max'],
#         )
#         utils.load_ckpt(self.model, hparams['ds_ckpt'], 'model', strict=True)
#
#         self.ds_params = [p for name, p in self.model.named_parameters() if
#                            ('midi_embed' not in name) and ('pitch_predictor' not in name)]
#
#         self.midi_params = [p for name, p in self.model.named_parameters() if
#                            ('midi_embed' in name) or ('pitch_predictor' in name)]
#
#     def build_optimizer(self, model):
#         self.optimizer = optimizer = torch.optim.AdamW(
#             self.midi_params,
#             lr=hparams['lr'],
#             betas=(hparams['optimizer_adam_beta1'], hparams['optimizer_adam_beta2']),
#             weight_decay=hparams['weight_decay'])
#         return optimizer
#
#     def run_model(self, model, sample, return_output=False, infer=False):
#         txt_tokens = sample['txt_tokens']  # [B, T_t]
#         target = sample['mels']  # [B, T_s, 80]
#         # mel2ph = sample['mel2ph'] if hparams['use_gt_dur'] else None # [B, T_s]
#         mel2ph = sample['mel2ph']
#         f0 = sample['f0']
#         uv = sample['uv']
#         energy = sample['energy']
#
#         pitch_midi = sample['pitch_midi']
#
#         spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
#         if hparams['pitch_type'] == 'cwt':
#             cwt_spec = sample[f'cwt_spec']
#             f0_mean = sample['f0_mean']
#             f0_std = sample['f0_std']
#             sample['f0_cwt'] = f0 = model.cwt2f0_norm(cwt_spec, f0_mean, f0_std, mel2ph)
#
#         output = model(txt_tokens, mel2ph=mel2ph, spk_embed=spk_embed,
#                        ref_mels=target, f0=f0, uv=uv, energy=energy, infer=infer, pitch_midi=pitch_midi)
#
#         losses = {}
#         # if 'diff_loss' in output:
#         #     losses['mel'] = output['diff_loss']
#         if hparams['use_pitch_embed']:
#             self.add_pitch_loss(output, sample, losses)
#         # if hparams['use_energy_embed']:
#         #     self.add_energy_loss(output['energy_pred'], energy, losses)
#         if not return_output:
#             return losses
#         else:
#             return losses, output
#
#     def validation_step(self, sample, batch_idx):
#         outputs = {}
#         txt_tokens = sample['txt_tokens']  # [B, T_t]
#
#         target = sample['mels']  # [B, T_s, 80]
#         energy = sample['energy']
#         # fs2_mel = sample['fs2_mels']
#         spk_embed = sample.get('spk_embed') if not hparams['use_spk_id'] else sample.get('spk_ids')
#         mel2ph = sample['mel2ph']
#         f0 = sample['f0']
#         uv = sample['uv']
#
#         pitch_midi = sample['pitch_midi']
#         # f0_midi = sample['f0_midi']
#
#         outputs['losses'] = {}
#
#         outputs['losses'], model_out = self.run_model(self.model, sample, return_output=True, infer=False)
#
#
#         outputs['total_loss'] = sum(outputs['losses'].values())
#         outputs['nsamples'] = sample['nsamples']
#         outputs = utils.tensors_to_scalars(outputs)
#         if batch_idx < hparams['num_valid_plots']:
#             model_out = self.model(
#                 txt_tokens, spk_embed=spk_embed, mel2ph=mel2ph, f0=f0, uv=uv, energy=energy, ref_mels=None, infer=True,
#                 pitch_midi=pitch_midi)
#
#             if hparams['use_pitch_embed']:
#                 self.plot_pitch(batch_idx, sample, model_out)
#         return outputs