from warp_core import WarpCore
from warp_core.utils import DTO_REQUIRED
from dataclasses import dataclass
import torch
import torchvision
from torch import nn, optim
from transformers import AutoTokenizer, CLIPModel, CLIPVisionModelWithProjection
from warmup_scheduler import GradualWarmupScheduler
import numpy as np

import sys
import os

from gdf import GDF, EpsilonTarget, CosineSchedule
from gdf import VPScaler, CosineTNoiseCond, DDPMSampler, P2LossWeight, AdaptiveLossWeight
from torchtools.transforms import SmartCrop

from modules.effnet import EfficientNetEncoder
from modules.stage_a import StageA

from modules.stage_b_700M import StageB as StageB_700M
from modules.stage_b_700M import ResBlock as ResBlock_700M, AttnBlock as AttnBlock_700M
from modules.stage_b_700M import TimestepBlock as TimestepBlock_700M, FeedForwardBlock as FeedForwardBlock_700M

from modules.stage_b_3B import StageB as StageB_3B
from modules.stage_b_3B import ResBlock as ResBlock_3B, AttnBlock as AttnBlock_3B
from modules.stage_b_3B import TimestepBlock as TimestepBlock_3B, FeedForwardBlock as FeedForwardBlock_3B

from train_templates import DataCore, TrainingCore

from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp.wrap import ModuleWrapPolicy

class WurstCore(TrainingCore, DataCore, WarpCore):
    # DTOs ---------------------------------------
    @dataclass(frozen=True)
    class ConfigDTO(TrainingCore.ConfigDTO, DataCore.ConfigDTO, WarpCore.ConfigDTO):
        # TRAINING PARAMS
        lr: float = DTO_REQUIRED
        warmup_updates: int = DTO_REQUIRED
        shift: float = DTO_REQUIRED

        # MODEL VERSION
        model_version: str = DTO_REQUIRED # 3BB or 700M
        clip_text_model_name: str = 'laion/CLIP-ViT-bigG-14-laion2B-39B-b160k'

        # CHECKPOINT PATHS
        stage_a_checkpoint_path: str = DTO_REQUIRED
        effnet_checkpoint_path: str = DTO_REQUIRED
        generator_checkpoint_path: str = None

        # gdf customization
        adaptive_loss_weight: str = None

    @dataclass(frozen=True)
    class ModelsDTO(TrainingCore.ModelsDTO, DataCore.ModelsDTO, WarpCore.ModelsDTO):
        effnet: nn.Module = DTO_REQUIRED
        stage_a : nn.Module = DTO_REQUIRED

    @dataclass(frozen=True)
    class SchedulersDTO(WarpCore.SchedulersDTO):
        generator: any = None

    @dataclass(frozen=True)
    class ExtrasDTO(TrainingCore.ExtrasDTO, DataCore.ExtrasDTO, WarpCore.ExtrasDTO):
        gdf: GDF = DTO_REQUIRED
        sampling_configs: dict = DTO_REQUIRED
        effnet_preprocess: torchvision.transforms.Compose = DTO_REQUIRED

    # @dataclass() # not frozen, means that fields are mutable. Doesn't support DTO_REQUIRED
    # class InfoDTO(TrainingCore.InfoDTO):
    #     adaptive_loss: dict = None

    # @dataclass(frozen=True)
    # class OptimizersDTO(TrainingCore.OptimizersDTO, WarpCore.OptimizersDTO):
    #     generator : any = DTO_REQUIRED

    # --------------------------------------------
    info: TrainingCore.InfoDTO
    config: ConfigDTO

    # Extras: gdf, transforms and preprocessors --------------------------------
    def setup_extras_pre(self) -> ExtrasDTO:
        gdf = GDF(
            schedule = CosineSchedule(clamp_range=[0.0001, 0.9999]),
            input_scaler = VPScaler(), target = EpsilonTarget(),
            noise_cond = CosineTNoiseCond(),
            loss_weight = AdaptiveLossWeight() if self.config.adaptive_loss_weight is True else P2LossWeight(),
        )
        sampling_configs = {"cfg": 1.5, "sampler": DDPMSampler(gdf), "shift": 1, "timesteps": 10}

        if self.info.adaptive_loss is not None:
            gdf.loss_weight.bucket_ranges = torch.tensor(self.info.adaptive_loss['bucket_ranges'])
            gdf.loss_weight.bucket_losses = torch.tensor(self.info.adaptive_loss['bucket_losses'])

        effnet_preprocess = torchvision.transforms.Compose([
            torchvision.transforms.Normalize(
                mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)
            )
        ])

        transforms = torchvision.transforms.Compose([
            torchvision.transforms.ToTensor(),
            torchvision.transforms.Resize(self.config.image_size, interpolation=torchvision.transforms.InterpolationMode.BILINEAR, antialias=True),
            SmartCrop(self.config.image_size, randomize_p=0.3, randomize_q=0.2)
        ])

        return self.ExtrasDTO(
            gdf=gdf,
            sampling_configs=sampling_configs,
            transforms=transforms,
            effnet_preprocess=effnet_preprocess,
            clip_preprocess=None
        )

    # Data --------------------------------
    def get_conditions(self, batch: dict, models: ModelsDTO, extras: ExtrasDTO, is_eval=False, is_unconditional=False, eval_image_embeds=False, return_fields=None):
        images = batch['images'].to(self.device)

        if is_eval and not is_unconditional:
            effnet_embeddings = models.effnet(extras.effnet_preprocess(images))
        else:
            effnet_factor = np.random.uniform(0.5, 1) # f64 to f32
            effnet_height, effnet_width = int(((images.size(-2)*effnet_factor)//32)*32), int(((images.size(-1)*effnet_factor)//32)*32)

            effnet_embeddings = torch.zeros(images.size(0), 16, effnet_height//32, effnet_width//32, device=self.device)
            if not is_eval:
                effnet_images = torchvision.transforms.functional.resize(images, (effnet_height, effnet_width), interpolation=torchvision.transforms.InterpolationMode.NEAREST)
                rand_idx = np.random.rand(len(images)) <= 0.9
                if any(rand_idx):
                    effnet_embeddings[rand_idx] = models.effnet(extras.effnet_preprocess(effnet_images[rand_idx]))

        conditions = super().get_conditions(
            batch, models, extras, is_eval, is_unconditional,
            eval_image_embeds, return_fields=return_fields or ['clip_text_pooled']
        )

        return {'effnet': effnet_embeddings, 'clip': conditions['clip_text_pooled']}

    # Models, Optimizers & Schedulers setup --------------------------------
    def setup_models(self, extras: ExtrasDTO) -> ModelsDTO:
        # EfficientNet encoder
        effnet = EfficientNetEncoder().to(self.device)
        effnet_checkpoint = torch.load(self.config.effnet_checkpoint_path, map_location=self.device)
        effnet.load_state_dict(effnet_checkpoint if 'state_dict' not in effnet_checkpoint else effnet_checkpoint['state_dict'])
        effnet.eval().requires_grad_(False)
        del effnet_checkpoint

        # vqGAN
        stage_a = StageA().to(self.device)
        stage_a_checkpoint = torch.load(self.config.stage_a_checkpoint_path, map_location=self.device)
        stage_a.load_state_dict(stage_a_checkpoint if 'state_dict' not in stage_a_checkpoint else stage_a_checkpoint['state_dict'])
        stage_a.eval().requires_grad_(False)
        del stage_a_checkpoint

        # Diffusion models
        if self.config.model_version == '3B':
            generator = StageB_3B().to(self.device)
            if self.config.ema_start_iters is not None:
                generator_ema = StageB_3B().to(self.device)
            else:
                generator_ema = None
        elif self.config.model_version == '700M':
            generator = StageB_700M().to(self.device)
            if self.config.ema_start_iters is not None:
                generator_ema = StageB_700M().to(self.device)
            else:
                generator_ema = None
        else:
            raise ValueError(f"Unknown model version {self.config.model_version}")

        if self.config.generator_checkpoint_path is not None:
            generator.load_state_dict(torch.load(self.config.generator_checkpoint_path, map_location=self.device))
        generator = self.load_model(generator, 'generator')

        if generator_ema is not None:
            generator_ema.load_state_dict(generator.state_dict())
            generator_ema = self.load_model(generator_ema, 'generator_ema')
            generator_ema.eval().requires_grad_(False)

        if self.config.use_fsdp:
            if self.config.model_version == '3B':
                fsdp_auto_wrap_policy = ModuleWrapPolicy([ResBlock_3B, AttnBlock_3B, TimestepBlock_3B, FeedForwardBlock_3B])
            else:
                fsdp_auto_wrap_policy = ModuleWrapPolicy([ResBlock_700M, AttnBlock_700M, TimestepBlock_700M, FeedForwardBlock_700M])
            generator = FSDP(generator, **self.fsdp_defaults, auto_wrap_policy=fsdp_auto_wrap_policy, device_id=self.device)
            if generator_ema is not None:
                generator_ema = FSDP(generator_ema, **self.fsdp_defaults, auto_wrap_policy=fsdp_auto_wrap_policy, device_id=self.device)

        # CLIP encoders
        clip_tokenizer = AutoTokenizer.from_pretrained(self.config.clip_text_model_name)
        clip_model = CLIPModel.from_pretrained(self.config.clip_text_model_name)
        clip_text_model = clip_model.text_model.to(self.device).eval().requires_grad_(False)
        clip_text_model_proj = clip_model.text_projection.to(self.device).eval().requires_grad_(False)
        del clip_model

        return self.ModelsDTO(
            effnet=effnet, stage_a=stage_a,
            generator=generator, generator_ema=generator_ema,

            clip_tokenizer=clip_tokenizer, clip_text_model=clip_text_model,
            clip_text_model_proj=clip_text_model_proj, clip_image_model=None
        )

    def setup_optimizers(self, extras: ExtrasDTO, models: ModelsDTO) -> TrainingCore.OptimizersDTO:
        optimizer = optim.AdamW(models.generator.parameters(), lr=self.config.lr) #, eps=1e-7, betas=(0.9, 0.95))
        optimizer = self.load_optimizer(optimizer, 'generator_optim', fsdp_model=models.generator if self.config.use_fsdp else None)
        return self.OptimizersDTO(generator=optimizer)

    def setup_schedulers(self, extras: ExtrasDTO, models: ModelsDTO, optimizers:TrainingCore.OptimizersDTO) -> SchedulersDTO:
        scheduler = GradualWarmupScheduler(optimizers.generator, multiplier=1, total_epoch=self.config.warmup_updates)
        scheduler.last_epoch = self.info.total_steps
        return self.SchedulersDTO(generator=scheduler)

    def _pyramid_noise(self, epsilon, size_range=None, levels=10, scale_mode='nearest'):
        epsilon = epsilon.clone()
        multipliers = [1]
        for i in range(1, levels):
    #         m = 0.5 / 2**i
            m = 0.75 ** i
            h, w = epsilon.size(-2)//(2**i), epsilon.size(-2)//(2**i)
            if size_range is None or (size_range[0] <= h <= size_range[1] or size_range[0] <= w <= size_range[1]):
                offset = torch.randn(epsilon.size(0), epsilon.size(1), h, w, device=self.device)
                epsilon = epsilon + torch.nn.functional.interpolate(offset, size=epsilon.shape[-2:], mode=scale_mode) * m
                multipliers.append(m)
            if h <= 1 or w <= 1:
                break
        epsilon = epsilon / sum([m**2 for m in multipliers])**0.5
        # epsilon = epsilon / epsilon.std()
        return epsilon

    # Training loop --------------------------------
    def forward_pass(self, data: WarpCore.DataDTO, extras: ExtrasDTO, models: ModelsDTO):
        batch = next(data.iterator)

        with torch.no_grad():
            conditions = self.get_conditions(batch, models, extras)
            latents = self.encode_latents(batch, models, extras)
            epsilon = torch.randn_like(latents)
            epsilon = self._pyramid_noise(epsilon, size_range=[1, 16])
            noised, noise, target, logSNR, noise_cond, loss_weight = extras.gdf.diffuse(latents, shift=1, loss_shift=1, epsilon=epsilon)

        with torch.cuda.amp.autocast(dtype=torch.bfloat16):
            pred = models.generator(noised, noise_cond, **conditions)
            loss = nn.functional.mse_loss(pred, target, reduction='none').mean(dim=[1, 2, 3])
            loss_adjusted = (loss * loss_weight).mean() / self.config.grad_accum_steps

        if isinstance(extras.gdf.loss_weight, AdaptiveLossWeight):
            extras.gdf.loss_weight.update_buckets(logSNR, loss)

        return loss, loss_adjusted

    def backward_pass(self, update, loss, loss_adjusted, models: ModelsDTO, optimizers: TrainingCore.OptimizersDTO, schedulers: SchedulersDTO):
        if update:
            loss_adjusted.backward()
            grad_norm = nn.utils.clip_grad_norm_(models.generator.parameters(), 1.0)
            optimizers_dict = optimizers.to_dict()
            for k in optimizers_dict:
                optimizers_dict[k].step()
            schedulers_dict = schedulers.to_dict()
            for k in schedulers_dict:
                schedulers_dict[k].step()
            for k in optimizers_dict:
                optimizers_dict[k].zero_grad(set_to_none=True)
            self.info.total_steps += 1
        else:
            with models.generator.no_sync():
                loss_adjusted.backward()

        return grad_norm

    def models_to_save(self):
        return ['generator', 'generator_ema']

    # LATENT ENCODING & PROCESSING ----------
    def encode_latents(self, batch: dict, models: ModelsDTO, extras: ExtrasDTO) -> torch.Tensor:
        images = batch['images'].to(self.device)
        return models.stage_a.encode(images)[0]

    def decode_latents(self, latents: torch.Tensor, batch: dict, models: ModelsDTO, extras: ExtrasDTO) -> torch.Tensor:
        return models.stage_a.decode(latents.float()).clamp(0, 1)

if __name__ == '__main__':
    print("Launching Script")
    warpcore = WurstCore(
        config_file_path=sys.argv[1] if len(sys.argv) > 1 else None,
        device=torch.device(int(os.environ.get("SLURM_LOCALID")))
    )
    # warp_core.fsdp_defaults['sharding_strategy'] = ShardingStrategy.NO_SHARD

    # RUN TRAINING
    warpcore()