from typing import Callable, Optional

import gin
import numpy as np
import pytorch_lightning as pl
import torch
import torch.nn as nn
from einops import rearrange
from sklearn.decomposition import PCA

import rave.core

from .balancer import Balancer
from .blocks import DiscreteEncoder, VariationalEncoder


class WarmupCallback(pl.Callback):

    def __init__(self) -> None:
        super().__init__()
        self.state = {'training_steps': 0}

    def on_train_batch_start(self, trainer, pl_module, batch,
                             batch_idx) -> None:
        if self.state['training_steps'] >= pl_module.warmup:
            pl_module.warmed_up = True
        self.state['training_steps'] += 1

    def state_dict(self):
        return self.state.copy()

    def load_state_dict(self, state_dict):
        self.state.update(state_dict)


class QuantizeCallback(WarmupCallback):

    def on_train_batch_start(self, trainer, pl_module, batch,
                             batch_idx) -> None:

        if pl_module.warmup_quantize is None: return

        if self.state['training_steps'] >= pl_module.warmup_quantize:
            if isinstance(pl_module.encoder, DiscreteEncoder):
                pl_module.encoder.enabled = torch.tensor(1).type_as(
                    pl_module.encoder.enabled)
        self.state['training_steps'] += 1


@gin.configurable
class RAVE(pl.LightningModule):

    def __init__(
        self,
        latent_size,
        pqmf,
        sampling_rate,
        encoder,
        decoder,
        discriminator,
        phase_1_duration,
        gan_loss,
        valid_signal_crop,
        feature_matching_fun,
        num_skipped_features,
        audio_distance: Callable[[], nn.Module],
        multiband_audio_distance: Callable[[], nn.Module],
        balancer: Callable[[], Balancer],
        warmup_quantize: Optional[int] = None,
        update_discriminator_every: int = 2,
        n_channels: int = 1
    ):
        super().__init__()

        self.pqmf = pqmf(n_channels=n_channels)
        self.encoder = encoder(beta=1.0, n_channels=n_channels)
        self.decoder = decoder(n_channels=n_channels)
        self.discriminator = discriminator(n_channels=n_channels)

        self.audio_distance = audio_distance()
        self.multiband_audio_distance = multiband_audio_distance()

        self.gan_loss = gan_loss

        self.register_buffer("latent_pca", torch.eye(latent_size))
        self.register_buffer("latent_mean", torch.zeros(latent_size))
        self.register_buffer("fidelity", torch.zeros(latent_size))

        self.latent_size = latent_size

        self.automatic_optimization = False

        # SCHEDULE
        self.warmup = phase_1_duration
        self.warmup_quantize = warmup_quantize
        self.balancer = balancer()

        self.warmed_up = False

        # CONSTANTS
        self.sr = sampling_rate
        self.valid_signal_crop = valid_signal_crop
        self.n_channels = n_channels
        self.feature_matching_fun = feature_matching_fun
        self.num_skipped_features = num_skipped_features
        self.update_discriminator_every = update_discriminator_every

        self.eval_number = 0

        self.register_buffer("receptive_field", torch.tensor([0, 0]).long())

    def configure_optimizers(self):
        gen_p = list(self.encoder.parameters())
        gen_p += list(self.decoder.parameters())
        dis_p = list(self.discriminator.parameters())

        gen_opt = torch.optim.Adam(gen_p, 1e-4, (.5, .9))
        dis_opt = torch.optim.Adam(dis_p, 1e-4, (.5, .9))

        return gen_opt, dis_opt

    def split_features(self, features):
        feature_true = []
        feature_fake = []
        for scale in features:
            true, fake = zip(*map(
                lambda x: torch.split(x, x.shape[0] // 2, 0),
                scale,
            ))
            feature_true.append(true)
            feature_fake.append(fake)
        return feature_true, feature_fake

    def training_step(self, batch, batch_idx):
        gen_opt, dis_opt = self.optimizers()
        # x = batch.unsqueeze(1)
        x = batch
        x.requires_grad = True

        batch_size = batch.shape[:-2]
        x = batch.reshape(-1, 1, batch.shape[-1])
        x_multiband = self.pqmf(x)
        x_multiband = x_multiband.reshape(*batch_size, -1, x_multiband.shape[-1])

        self.encoder.set_warmed_up(self.warmed_up)
        self.decoder.set_warmed_up(self.warmed_up)

        # ENCODE INPUT
        z_pre_reg = self.encoder(x_multiband)
        z, reg = self.encoder.reparametrize(z_pre_reg)[:2]

        # DECODE LATENT
        y_multiband = self.decoder(z)

        if self.valid_signal_crop and self.receptive_field.sum():
            x_multiband = rave.core.valid_signal_crop(
                x_multiband,
                *self.receptive_field,
            )
            y_multiband = rave.core.valid_signal_crop(
                y_multiband,
                *self.receptive_field,
            )

        # DISTANCE BETWEEN INPUT AND OUTPUT
        multiband_distance = self.multiband_audio_distance(
            x_multiband, y_multiband)

        x_multiband_tmp = x_multiband.reshape(x_multiband.shape[0] * self.n_channels, -1, x_multiband.shape[-1])
        y_multiband_tmp = y_multiband.reshape(y_multiband.shape[0] * self.n_channels, -1, y_multiband.shape[-1])
        x = self.pqmf.inverse(x_multiband_tmp)
        y = self.pqmf.inverse(y_multiband_tmp)
        x = x.reshape(*batch_size, self.n_channels, -1)
        y = y.reshape(*batch_size, self.n_channels, -1)

        fullband_distance = self.audio_distance(x, y)

        distances = {}

        for k, v in multiband_distance.items():
            distances[f'multiband_{k}'] = v
        for k, v in fullband_distance.items():
            distances[f'fullband_{k}'] = v

        feature_matching_distance = 0.

        if self.warmed_up:  # DISCRIMINATION
            xy = torch.cat([x, y], 0)
            features = self.discriminator(xy)

            feature_true, feature_fake = self.split_features(features)

            loss_dis = 0
            loss_adv = 0

            pred_true = 0
            pred_fake = 0

            for scale_true, scale_fake in zip(feature_true, feature_fake):
                current_feature_distance = sum(
                    map(
                        self.feature_matching_fun,
                        scale_true[self.num_skipped_features:-1],
                        scale_fake[self.num_skipped_features:-1],
                    )) / len(scale_true[self.num_skipped_features:-1])

                feature_matching_distance = feature_matching_distance + current_feature_distance

                _dis, _adv = self.gan_loss(scale_true[-1], scale_fake[-1])

                pred_true = pred_true + scale_true[-1].mean()
                pred_fake = pred_fake + scale_fake[-1].mean()

                loss_dis = loss_dis + _dis
                loss_adv = loss_adv + _adv

            feature_matching_distance = feature_matching_distance / len(
                feature_true)

        else:
            pred_true = torch.tensor(0.).to(x)
            pred_fake = torch.tensor(0.).to(x)
            loss_dis = torch.tensor(0.).to(x)
            loss_adv = torch.tensor(0.).to(x)

        # COMPOSE GEN LOSS
        loss_gen = {}

        loss_gen.update(distances)

        if reg.item():
            loss_gen['regularization'] = reg

        if self.warmed_up:
            loss_gen['feature_matching'] = feature_matching_distance
            loss_gen['adversarial'] = loss_adv

        # OPTIMIZATION
        if batch_idx % self.update_discriminator_every and self.warmed_up:
            dis_opt.zero_grad()
            loss_dis.backward()
            dis_opt.step()
        else:
            gen_opt.zero_grad()
            self.balancer.backward(
                loss_gen,
                {
                    'default': y,
                    'multiband_waveform_distance': y_multiband,
                    'multiband_spectral_distance': y_multiband,
                    'regularization': z_pre_reg,
                },
            )
            gen_opt.step()

        # LOGGING
        if self.warmed_up:
            self.log("loss_dis", loss_dis)
            self.log("pred_true", pred_true.mean())
            self.log("pred_fake", pred_fake.mean())

        self.log_dict(loss_gen)

    def encode(self, x):
        x = self.pqmf(x)
        z, = self.encoder.reparametrize(self.encoder(x))[:1]
        return z

    def decode(self, z):
        y = self.decoder(z)
        y = self.pqmf.inverse(y)
        return y

    def forward(self, x):
        return self.decode(self.encode(x))

    def validation_step(self, batch, batch_idx):
        # x = batch.unsqueeze(1)
        x = batch
        batch_size = x.shape[:-2]
        x = x.reshape(-1, 1, x.shape[-1])
        x = self.pqmf(x)
        x = x.reshape(*batch_size, -1, x.shape[-1])
        z = self.encoder(x)

        if isinstance(self.encoder, VariationalEncoder):
            mean = torch.split(z, z.shape[1] // 2, 1)[0]
        else:
            mean = None

        z = self.encoder.reparametrize(z)[0]
        y = self.decoder(z)

        x = x.reshape(x.shape[0] * self.n_channels, -1, x.shape[-1])
        y = y.reshape(y.shape[0] * self.n_channels, -1, y.shape[-1])
        x = self.pqmf.inverse(x)
        y = self.pqmf.inverse(y)
        x = x.reshape(*batch_size, self.n_channels, -1)
        y = y.reshape(*batch_size, self.n_channels, -1)

        distance = self.audio_distance(x, y)

        full_distance = sum(distance.values())

        if self.trainer is not None:
            self.log('validation', full_distance)

        return torch.cat([x, y], -1), mean

    def validation_epoch_end(self, out):
        if not self.receptive_field.sum():
            print("Computing receptive field for this configuration...")
            lrf, rrf = rave.core.get_rave_receptive_field(self, n_channels=self.n_channels)
            self.receptive_field[0] = lrf
            self.receptive_field[1] = rrf
            print(
                f"Receptive field: {1000*lrf/self.sr:.2f}ms <-- x --> {1000*rrf/self.sr:.2f}ms"
            )

        if not len(out): return

        audio, z = list(zip(*out))
        audio = list(map(lambda x: x.cpu(), audio))

        # LATENT SPACE ANALYSIS
        if not self.warmed_up and isinstance(self.encoder, VariationalEncoder):
            z = torch.cat(z, 0)
            z = rearrange(z, "b c t -> (b t) c")

            self.latent_mean.copy_(z.mean(0))
            z = z - self.latent_mean

            pca = PCA(z.shape[-1]).fit(z.cpu().numpy())

            components = pca.components_
            components = torch.from_numpy(components).to(z)
            self.latent_pca.copy_(components)

            var = pca.explained_variance_ / np.sum(pca.explained_variance_)
            var = np.cumsum(var)

            self.fidelity.copy_(torch.from_numpy(var).to(self.fidelity))

            var_percent = [.8, .9, .95, .99]
            for p in var_percent:
                self.log(
                    f"fidelity_{p}",
                    np.argmax(var > p).astype(np.float32),
                )

        y = torch.cat(audio, 0)[:8].reshape(-1)
        self.logger.experiment.add_audio("audio_val", y, self.eval_number,
                                         self.sr)
        self.eval_number += 1
