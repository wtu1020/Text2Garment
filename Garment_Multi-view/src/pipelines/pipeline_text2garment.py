import inspect
from dataclasses import dataclass
from typing import Callable, List, Optional, Union

import numpy as np
import torch
from diffusers import DiffusionPipeline
from diffusers.image_processor import VaeImageProcessor
from diffusers.schedulers import (DDIMScheduler, DPMSolverMultistepScheduler,
                                  EulerAncestralDiscreteScheduler,
                                  EulerDiscreteScheduler, LMSDiscreteScheduler,
                                  PNDMScheduler)
from diffusers.utils import BaseOutput, is_accelerate_available
from diffusers.utils.torch_utils import randn_tensor
from einops import rearrange
from tqdm import tqdm
from transformers import CLIPImageProcessor
from src.models.mutual_self_attention import ReferenceAttentionControl
rgb_mean = 0.484496
rgb_std = 1.229314
normal_mean = -0.219865
normal_std = 1.445059

import cv2
import numpy as np
from sklearn.neighbors import KDTree

def rescale_noise_cfg(noise_cfg, noise_pred_text, guidance_rescale=0.0):

    std_text = noise_pred_text.std(dim=list(range(1, noise_pred_text.ndim)), keepdim=True)
    std_cfg = noise_cfg.std(dim=list(range(1, noise_cfg.ndim)), keepdim=True)
    noise_pred_rescaled = noise_cfg * (std_text / std_cfg)
    noise_cfg = guidance_rescale * noise_pred_rescaled + (1 - guidance_rescale) * noise_cfg
    return noise_cfg

@dataclass
class Text2GarmentPipelineOutput(BaseOutput):
    rgb_videos: Union[torch.Tensor, np.ndarray]
    normal_videos: Union[torch.Tensor, np.ndarray]

class Text2GarmentPipeline(DiffusionPipeline):
    _optional_components = []

    def __init__(
        self,
        vae,
        image_encoder,
        reference_unet,
        denoising_unet,
        semantic_guider,
        normal_guider,
        scheduler: Union[
            DDIMScheduler,
            PNDMScheduler,
            LMSDiscreteScheduler,
            EulerDiscreteScheduler,
            EulerAncestralDiscreteScheduler,
            DPMSolverMultistepScheduler,
        ],
        unet_attention_mode="read_concat_view",
        image_proj_model=None,
        tokenizer=None,
        text_encoder=None,
    ):
        super().__init__()
        self.register_modules(
            vae=vae,
            image_encoder=image_encoder,
            reference_unet=reference_unet,
            denoising_unet=denoising_unet,
            semantic_guider=semantic_guider,
            normal_guider=normal_guider,
            scheduler=scheduler,
            image_proj_model=image_proj_model,
            tokenizer=tokenizer,
            text_encoder=text_encoder,
        )
        self.vae_scale_factor = 2 ** (len(self.vae.config.block_out_channels) - 1)
        self.clip_image_processor = CLIPImageProcessor()
        self.anc_image_processor = VaeImageProcessor(
            vae_scale_factor=self.vae_scale_factor, do_convert_rgb=True
        )
        self.cond_image_processor = VaeImageProcessor(
            vae_scale_factor=self.vae_scale_factor,
            do_convert_rgb=True,
            do_normalize=False,
        )
        self.unet_attention_mode = unet_attention_mode

    def enable_vae_slicing(self):
        self.vae.enable_slicing()

    def disable_vae_slicing(self):
        self.vae.disable_slicing()

    def enable_sequential_cpu_offload(self, gpu_id=0):
        if is_accelerate_available():
            from accelerate import cpu_offload
        else:
            raise ImportError("Please install accelerate via `pip install accelerate`")

        device = torch.device(f"cuda:{gpu_id}")

        for cpu_offloaded_model in [self.unet, self.text_encoder, self.vae]:
            if cpu_offloaded_model is not None:
                cpu_offload(cpu_offloaded_model, device)

    @property
    def _execution_device(self):
        if self.device != torch.device("meta") or not hasattr(self.unet, "_hf_hook"):
            return self.device
        for module in self.unet.modules():
            if (
                hasattr(module, "_hf_hook")
                and hasattr(module._hf_hook, "execution_device")
                and module._hf_hook.execution_device is not None
            ):
                return torch.device(module._hf_hook.execution_device)
        return self.device

    def decode_latents(self, latents, latents_type="rgb"): # b c f h w
        video_length = latents.shape[2]
        if latents_type == "rgb":
            latents = latents
        elif latents_type == "normal":
            latents = (latents - rgb_mean) / rgb_std * normal_std + normal_mean
        else:
            raise ValueError
        latents = 1 / 0.18215 * latents
        latents = rearrange(latents, "b c f h w -> (b f) c h w")
        video = []
        for frame_idx in tqdm(range(latents.shape[0])):
            video.append(self.vae.decode(latents[frame_idx : frame_idx + 1]).sample)
        video = torch.cat(video)
        video = rearrange(video, "(b f) c h w -> b c f h w", f=video_length)
        video = (video / 2 + 0.5).clamp(0, 1)
        video = video.cpu().float().numpy()
        return video

    def prepare_extra_step_kwargs(self, generator, eta):

        accepts_eta = "eta" in set(
            inspect.signature(self.scheduler.step).parameters.keys()
        )
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta

        accepts_generator = "generator" in set(
            inspect.signature(self.scheduler.step).parameters.keys()
        )
        if accepts_generator:
            extra_step_kwargs["generator"] = generator
        return extra_step_kwargs

    def prepare_latents(
        self,
        batch_size,
        num_channels_latents,
        width,
        height,
        video_length,
        dtype,
        device,
        generator,
        latents=None,
    ):
        shape = (
            batch_size,
            num_channels_latents,
            video_length,
            height // self.vae_scale_factor,
            width // self.vae_scale_factor,
        )
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f"You have passed a list of generators of length {len(generator)}, but requested an effective batch"
                f" size of {batch_size}. Make sure the batch size matches the length of the generators."
            )

        if latents is None:
            latents = randn_tensor(
                shape, generator=generator, device=device, dtype=dtype
            )
        else:
            latents = latents.to(device)

        latents = latents * self.scheduler.init_noise_sigma
        return latents

    def _encode_prompt(
        self,
        prompt,
        device,
        num_videos_per_prompt,
        do_classifier_free_guidance,
        negative_prompt,
    ):
        batch_size = len(prompt) if isinstance(prompt, list) else 1

        text_inputs = self.tokenizer(
            prompt,
            padding="max_length",
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors="pt",
        )
        text_input_ids = text_inputs.input_ids
        untruncated_ids = self.tokenizer(
            prompt, padding="longest", return_tensors="pt"
        ).input_ids

        if untruncated_ids.shape[-1] >= text_input_ids.shape[-1] and not torch.equal(
            text_input_ids, untruncated_ids
        ):
            removed_text = self.tokenizer.batch_decode(
                untruncated_ids[:, self.tokenizer.model_max_length - 1 : -1]
            )

        if (
            hasattr(self.text_encoder.config, "use_attention_mask")
            and self.text_encoder.config.use_attention_mask
        ):
            attention_mask = text_inputs.attention_mask.to(device)
        else:
            attention_mask = None

        text_embeddings = self.text_encoder(
            text_input_ids.to(device),
            attention_mask=attention_mask,
        )
        text_embeddings = text_embeddings[0]

        bs_embed, seq_len, _ = text_embeddings.shape
        text_embeddings = text_embeddings.repeat(1, num_videos_per_prompt, 1)
        text_embeddings = text_embeddings.view(
            bs_embed * num_videos_per_prompt, seq_len, -1
        )

        if do_classifier_free_guidance:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [""] * batch_size
            elif type(prompt) is not type(negative_prompt):
                raise TypeError(
                    f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} !="
                    f" {type(prompt)}."
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`:"
                    f" {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches"
                    " the batch size of `prompt`."
                )
            else:
                uncond_tokens = negative_prompt

            max_length = text_input_ids.shape[-1]
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=max_length,
                truncation=True,
                return_tensors="pt",
            )

            if (
                hasattr(self.text_encoder.config, "use_attention_mask")
                and self.text_encoder.config.use_attention_mask
            ):
                attention_mask = uncond_input.attention_mask.to(device)
            else:
                attention_mask = None

            uncond_embeddings = self.text_encoder(
                uncond_input.input_ids.to(device),
                attention_mask=attention_mask,
            )
            uncond_embeddings = uncond_embeddings[0]

            seq_len = uncond_embeddings.shape[1]
            uncond_embeddings = uncond_embeddings.repeat(1, num_videos_per_prompt, 1)
            uncond_embeddings = uncond_embeddings.view(
                batch_size * num_videos_per_prompt, seq_len, -1
            )

            text_embeddings = torch.cat([uncond_embeddings, text_embeddings])

        return text_embeddings

    @torch.no_grad()
    def __call__(
        self,
        anc_rgb_image,
        anc_normal_image,
        cond_semantic_images, 
        cond_normal_images,
        camera,
        anc_camera,
        width,
        height,
        video_length,
        num_inference_steps,
        guidance_scale,
        smplx_guidance_scale,
        guidance_rescale: float = 0.7,
        num_images_per_prompt: int = 1,
        eta: float = 0.0,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        output_type: Optional[str] = "tensor",
        return_dict: bool = True,
        callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
        callback_steps: Optional[int] = 1,
        **kwargs,
    ):
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor

        device = self._execution_device

        do_classifier_free_guidance = guidance_scale > 1.0
        do_smplx_classifier_free_guidance = smplx_guidance_scale > 0.0 # whether to use smplx guidance

        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps

        batch_size = 1

        encoder_hidden_states = None
                
        reference_control_writer = ReferenceAttentionControl(
            self.reference_unet,
            do_classifier_free_guidance=do_classifier_free_guidance,
            mode="write",
            batch_size=batch_size, # 1
            fusion_blocks="full",
        )
        reference_control_reader = ReferenceAttentionControl(
            self.denoising_unet,
            do_classifier_free_guidance=do_classifier_free_guidance,
            mode=self.unet_attention_mode, 
            batch_size=batch_size, # 1
            fusion_blocks="full",
        )

        num_channels_latents = self.denoising_unet.in_channels
        latents = self.prepare_latents(
            batch_size * num_images_per_prompt, 
            num_channels_latents,
            width,
            height,
            video_length,
            self.vae.dtype,
            device,
            generator,
        )
        latents_list = []
        for i in range(self.denoising_unet.branch_num):
            latents_list.append(
                self.prepare_latents( 
                    batch_size * num_images_per_prompt, 
                    num_channels_latents,
                    width,
                    height,
                    video_length,
                    self.vae.dtype,
                    device,
                    generator,
                )
            )

        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

        anc_rgb_tensor = self.anc_image_processor.preprocess(
            anc_rgb_image, height=height, width=width
        ).to(dtype=self.vae.dtype, device=self.vae.device)
        anc_rgb_latents = self.vae.encode(anc_rgb_tensor).latent_dist.mean
        anc_rgb_latents = anc_rgb_latents * 0.18215
        anc_normal_tensor = self.anc_image_processor.preprocess(
            anc_normal_image, height=height, width=width
        ).to(dtype=self.vae.dtype, device=self.vae.device)
        anc_normal_latents = self.vae.encode(anc_normal_tensor).latent_dist.mean
        anc_normal_latents = anc_normal_latents * 0.18215
        anc_normal_latents = (anc_normal_latents - normal_mean) / normal_std * rgb_std + rgb_mean
        
        if self.semantic_guider is not None and cond_semantic_images is not None:
            cond_semantic_tensor_list = []
            for cond_semantic_image in cond_semantic_images:
                cond_semantic_tensor = (
                    torch.from_numpy(np.array(cond_semantic_image.resize((width, height)))) / 255.0 
                ).permute(2, 0, 1).unsqueeze(1)  # (c, 1, h, w)
                cond_semantic_tensor_list.append(cond_semantic_tensor)
            cond_semantic_tensor = torch.cat(cond_semantic_tensor_list, dim=1).unsqueeze(0)
            cond_semantic_tensor = cond_semantic_tensor.to(
                device=device, dtype=self.semantic_guider.dtype
            )
            semantic_fea = self.semantic_guider(cond_semantic_tensor)
            semantic_fea = (
                torch.cat([semantic_fea] * 2) if do_classifier_free_guidance else semantic_fea
            )
        else:
            semantic_fea = None
        
        if self.normal_guider is not None and cond_normal_images is not None:
            cond_normal_tensor_list = []
            for cond_normal_image in cond_normal_images:
                cond_normal_tensor = (
                    torch.from_numpy(np.array(cond_normal_image.resize((width, height)))) / 255.0
                ).permute(2, 0, 1).unsqueeze(1)
                cond_normal_tensor_list.append(cond_normal_tensor)
            cond_normal_tensor = torch.cat(cond_normal_tensor_list, dim=1).unsqueeze(0)
            cond_normal_tensor = cond_normal_tensor.to(
                device=device, dtype=self.normal_guider.dtype
            )
            normal_fea = self.normal_guider(cond_normal_tensor)
            normal_fea = (
                torch.cat([normal_fea] * 2) if do_classifier_free_guidance else normal_fea
            )
        else:
            normal_fea = None


        camera = torch.from_numpy(camera[:,:3,:3]).reshape(video_length, -1).unsqueeze(0).to(device=device, dtype=latents.dtype)
        camera = torch.cat([camera] * 2) if do_classifier_free_guidance else camera

        anc_camera = torch.from_numpy(anc_camera[:3, :3].reshape(-1))[None, :].to(device=device, dtype=latents.dtype)
        anc_camera = torch.cat([anc_camera] * 2) if do_classifier_free_guidance else anc_camera
        
        num_warmup_steps = len(timesteps) - num_inference_steps * self.scheduler.order
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                if i == 0:
                    self.reference_unet(
                        anc_rgb_latents.repeat((2 if do_classifier_free_guidance else 1), 1, 1, 1),
                        [anc_normal_latents.repeat((2 if do_classifier_free_guidance else 1), 1, 1, 1)],
                        torch.zeros_like(t),
                        camera=anc_camera,
                        encoder_hidden_states=encoder_hidden_states,
                        return_dict=False,
                    )
                    reference_control_reader.update(reference_control_writer)

                latent_model_input = (torch.cat([latents] * 2) if do_classifier_free_guidance else latents)
                latent_model_input = self.scheduler.scale_model_input(latent_model_input, t) 
                latent_model_input_list = []
                for idx in range(self.denoising_unet.branch_num):
                    latent_model_input_list.append(
                        (torch.cat([latents_list[idx]] * 2) if do_classifier_free_guidance else latents_list[idx])
                    )
                    latent_model_input_list[idx] = self.scheduler.scale_model_input(latent_model_input_list[idx], t)

                if smplx_guidance_scale > 0:
                    noise_pred = self.denoising_unet(
                        latent_model_input,
                        latent_model_input_list,
                        t,
                        encoder_hidden_states=encoder_hidden_states,
                        camera=camera,
                        pose_cond_fea=semantic_fea,
                        pose_cond_fea_list=[normal_fea],
                        return_dict=False,
                    )[0]
                    if do_classifier_free_guidance:
                        noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                        noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)
                    if do_classifier_free_guidance and guidance_rescale > 0.0:
                        noise_pred = rescale_noise_cfg(noise_pred, noise_pred_text, guidance_rescale=guidance_rescale)  
                
                noise_pred_wo_smplx = self.denoising_unet(
                    latent_model_input,
                    latent_model_input_list,
                    t,
                    encoder_hidden_states=encoder_hidden_states,
                    camera=camera,
                    pose_cond_fea=None,
                    pose_cond_fea_list=[None],
                    return_dict=False,
                )[0]
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred_wo_smplx.chunk(2)
                    noise_pred_wo_smplx = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond) # (b=1, 2c=8, f, h=64, w=64)
                if do_classifier_free_guidance and guidance_rescale > 0.0: 
                    noise_pred_wo_smplx = rescale_noise_cfg(noise_pred_wo_smplx, noise_pred_text, guidance_rescale=guidance_rescale)
                
                '''perform smplx guidance'''
                if smplx_guidance_scale > 0:
                    noise_w_smplx = noise_pred 
                    noise_pred = noise_pred_wo_smplx + smplx_guidance_scale * (noise_w_smplx - noise_pred_wo_smplx)
                    if do_classifier_free_guidance and guidance_rescale > 0.0: 
                        noise_pred = rescale_noise_cfg(noise_pred, noise_w_smplx, guidance_rescale=guidance_rescale)
                else:
                    noise_pred = noise_pred_wo_smplx
                
                noise_pred_list = [noise_pred[:, 4:8]]
                noise_pred = noise_pred[:, :4]
                

                latents = self.scheduler.step( 
                    noise_pred, t, latents, **extra_step_kwargs, return_dict=False
                )[0]
                assert len(latents_list) == len(noise_pred_list) == self.denoising_unet.branch_num
                for idx in range(self.denoising_unet.branch_num):
                    latents_list[idx] = self.scheduler.step(
                        noise_pred_list[idx], t, latents_list[idx], **extra_step_kwargs, return_dict=False
                    )[0]
                
                if i == len(timesteps) - 1 or (
                    (i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0
                ):
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        step_idx = i // getattr(self.scheduler, "order", 1)
                        callback(step_idx, t, latents)

            reference_control_reader.clear()
            reference_control_writer.clear()

        rgb_latents = latents
        normal_latents = latents_list[0]

        rgb_images = self.decode_latents(rgb_latents, latents_type="rgb")
        normal_images = self.decode_latents(normal_latents, latents_type="normal")
        
        if output_type == "tensor":
            rgb_images = torch.from_numpy(rgb_images)
            normal_images = torch.from_numpy(normal_images)
            
        if not return_dict:
            return (rgb_images, normal_images)

        return Text2GarmentPipelineOutput(rgb_videos=rgb_images, normal_videos=normal_images)