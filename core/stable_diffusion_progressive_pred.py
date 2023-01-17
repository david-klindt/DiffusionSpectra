import json
import math
import os
from os.path import join
from tqdm import tqdm
import torch
import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt
from diffusers import pipelines, StableDiffusionPipeline
from core.utils.plot_utils import show_imgrid, save_imgrid, saveallforms, to_imgrid

from core.diffusion_geometry_lib import proj2subspace, proj2orthospace, subspace_variance, \
        trajectory_geometry_pipeline, diff_cosine_mat_analysis, \
        latent_PCA_analysis, latent_diff_PCA_analysis, PCA_data_visualize, ldm_PCA_data_visualize
from core.diffusion_traj_analysis_lib import \
    denorm_std, denorm_var, denorm_sample_std, \
    latents_to_image, latentvecs_to_image, \
    compute_save_diff_imgs_diff, compute_save_diff_imgs_ldm, plot_diff_matrix, visualize_traj_2d_cycle
#%%
# exproot = r"/home/binxuwang/insilico_exp/Diffusion_Hessian/StableDiffusion"
pipe = StableDiffusionPipeline.from_pretrained(
    "runwayml/stable-diffusion-v1-5",
    revision="fp16",
    torch_dtype=torch.float16,
)
pipe = pipe.to("cuda")
pipe.enable_attention_slicing()
pipe.text_encoder.requires_grad_(False)
pipe.unet.requires_grad_(False)
pipe.vae.requires_grad_(False)
# pipeline.to(torch.half)
def dummy_checker(images, **kwargs): return images, False

pipe.safety_checker = dummy_checker
#%%
# prompt = "a portrait of an aristocrat"
# #  in Edgar Degas style
# tsteps = 51
# # for seed in range(100, 125):
# latents_reservoir = []
# @torch.no_grad()
# def save_latents(i, t, latents):
#     latents_reservoir.append(latents.detach().cpu())
#
# seed = 105
# out = pipe(prompt, callback=save_latents,
#            num_inference_steps=tsteps, generator=torch.cuda.manual_seed(seed))
# out.images[0].show()
# latents_reservoir = torch.cat(latents_reservoir, dim=0)
#%%
from typing import Callable, List, Optional, Union
@torch.no_grad()
def SD_sampler(
        pipe,
        prompt: Union[str, List[str]],
        height: Optional[int] = None,
        width: Optional[int] = None,
        num_inference_steps: int = 50,
        guidance_scale: float = 7.5,
        negative_prompt: Optional[Union[str, List[str]]] = None,
        num_images_per_prompt: Optional[int] = 1,
        eta: float = 0.0,
        generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
        latents: Optional[torch.FloatTensor] = None,
        output_type: Optional[str] = "pil",
        return_dict: bool = True,
        callback: Optional[Callable[[int, int, torch.FloatTensor], None]] = None,
        callback_steps: Optional[int] = 1,
):
    r"""
    Function invoked when calling the pipeline for generation.

    Args:
        prompt (`str` or `List[str]`):
            The prompt or prompts to guide the image generation.
        height (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
            The height in pixels of the generated image.
        width (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
            The width in pixels of the generated image.
        num_inference_steps (`int`, *optional*, defaults to 50):
            The number of denoising steps. More denoising steps usually lead to a higher quality image at the
            expense of slower inference.
        guidance_scale (`float`, *optional*, defaults to 7.5):
            Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
            `guidance_scale` is defined as `w` of equation 2. of [Imagen
            Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
            1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
            usually at the expense of lower image quality.
        negative_prompt (`str` or `List[str]`, *optional*):
            The prompt or prompts not to guide the image generation. Ignored when not using guidance (i.e., ignored
            if `guidance_scale` is less than `1`).
        num_images_per_prompt (`int`, *optional*, defaults to 1):
            The number of images to generate per prompt.
        eta (`float`, *optional*, defaults to 0.0):
            Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
            [`schedulers.DDIMScheduler`], will be ignored for others.
        generator (`torch.Generator`, *optional*):
            One or a list of [torch generator(s)](https://pytorch.org/docs/stable/generated/torch.Generator.html)
            to make generation deterministic.
        latents (`torch.FloatTensor`, *optional*):
            Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for image
            generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
            tensor will ge generated by sampling using the supplied random `generator`.
        output_type (`str`, *optional*, defaults to `"pil"`):
            The output format of the generate image. Choose between
            [PIL](https://pillow.readthedocs.io/en/stable/): `PIL.Image.Image` or `np.array`.
        return_dict (`bool`, *optional*, defaults to `True`):
            Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
            plain tuple.
        callback (`Callable`, *optional*):
            A function that will be called every `callback_steps` steps during inference. The function will be
            called with the following arguments: `callback(step: int, timestep: int, latents: torch.FloatTensor)`.
        callback_steps (`int`, *optional*, defaults to 1):
            The frequency at which the `callback` function will be called. If not specified, the callback will be
            called at every step.

    Returns:
        [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
        [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] if `return_dict` is True, otherwise a `tuple.
        When returning a tuple, the first element is a list with the generated images, and the second element is a
        list of `bool`s denoting whether the corresponding generated image likely represents "not-safe-for-work"
        (nsfw) content, according to the `safety_checker`.
    """
    # 0. Default height and width to unet
    height = height or pipe.unet.config.sample_size * pipe.vae_scale_factor
    width = width or pipe.unet.config.sample_size * pipe.vae_scale_factor

    # 1. Check inputs. Raise error if not correct
    pipe.check_inputs(prompt, height, width, callback_steps)

    # 2. Define call parameters
    batch_size = 1 if isinstance(prompt, str) else len(prompt)
    device = pipe._execution_device
    # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
    # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
    # corresponds to doing no classifier free guidance.
    do_classifier_free_guidance = guidance_scale > 1.0

    # 3. Encode input prompt
    text_embeddings = pipe._encode_prompt(
        prompt, device, num_images_per_prompt, do_classifier_free_guidance, negative_prompt
    )

    # 4. Prepare timesteps
    pipe.scheduler.set_timesteps(num_inference_steps, device=device)
    timesteps = pipe.scheduler.timesteps

    # 5. Prepare latent variables
    num_channels_latents = pipe.unet.in_channels
    latents = pipe.prepare_latents(
        batch_size * num_images_per_prompt,
        num_channels_latents,
        height,
        width,
        text_embeddings.dtype,
        device,
        generator,
        latents,
    )

    # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
    extra_step_kwargs = pipe.prepare_extra_step_kwargs(generator, eta)
    latents_trajectory = []
    noise_pred_trajectory = []
    noise_pred_uncond_trajectory = []
    noise_pred_text_trajectory = []
    latents_trajectory.append(latents.detach().cpu())
    # 7. Denoising loop
    num_warmup_steps = len(timesteps) - num_inference_steps * pipe.scheduler.order
    with pipe.progress_bar(total=num_inference_steps) as progress_bar:
        for i, t in enumerate(timesteps):
            # expand the latents if we are doing classifier free guidance
            latent_model_input = torch.cat([latents] * 2) if do_classifier_free_guidance else latents
            latent_model_input = pipe.scheduler.scale_model_input(latent_model_input, t)

            # predict the noise residual
            noise_pred = pipe.unet(latent_model_input, t, encoder_hidden_states=text_embeddings).sample

            # perform guidance
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

            # compute the previous noisy sample x_t -> x_t-1
            latents = pipe.scheduler.step(noise_pred, t, latents, **extra_step_kwargs).prev_sample

            # the added code, to save the trajectory of the latents and the noise predictions
            latents_trajectory.append(latents.detach().cpu())
            noise_pred_trajectory.append(noise_pred.detach().cpu())
            if do_classifier_free_guidance:
                noise_pred_uncond_trajectory.append(noise_pred_uncond.detach().cpu())
                noise_pred_text_trajectory.append(noise_pred_text.detach().cpu())
            # call the callback, if provided
            if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % pipe.scheduler.order == 0):
                progress_bar.update()
                if callback is not None and i % callback_steps == 0:
                    callback(i, t, latents)

    # 8. Post-processing
    image = pipe.decode_latents(latents)

    # 9. Run safety checker

    # 10. Convert to PIL
    if output_type == "pil":
        image = pipe.numpy_to_pil(image)

    noise_pred_trajectory = torch.stack(noise_pred_trajectory)
    latents_trajectory = torch.stack(latents_trajectory)
    if do_classifier_free_guidance:
        noise_pred_uncond_trajectory = torch.stack(noise_pred_uncond_trajectory)
        noise_pred_text_trajectory = torch.stack(noise_pred_text_trajectory)
        return image, latents_trajectory, noise_pred_trajectory, noise_pred_uncond_trajectory, noise_pred_text_trajectory
    else:
        return image, latents_trajectory, noise_pred_trajectory


def denorm_sample_renorm(x, mu, std):
    return ((x - x.mean(dim=(1,2,3), keepdims=True)) / x.std(dim=(1,2,3), keepdims=True) * std + mu)
#%%
#%%
import matplotlib
matplotlib.use('Agg')
# use the interactive backend

# matplotlib.use('module://backend_interagg')
#%%
import platform
if platform.system() == "Windows":
    saveroot = r"F:\insilico_exps\Diffusion_traj\StableDiffusion"
elif platform.system() == "Linux":
    saveroot = r"/home/binxuwang/insilico_exp/Diffusion_traj/StableDiffusion"
else:
    raise RuntimeError("Unknown system")

prompt_dir_pair = [
    ("a portrait of an aristocrat", "portrait_aristocrat"),
    ("a portrait of an light bulb", "portrait_lightbulb"),
    ("a large box containing an apple and a toy teddy bear", "box_apple_bear"),
    ("a photo of a cat sitting with a dog on a cozy couch", "cat_dog_couch"),
    ("a CG art of a brain composed of eletronic wires and circuits", "brain_wire_circuits"),
    ("a handsome cat dancing Tango with a female dancer in Monet style", "cat_tango_dancer"),
    ("a bug crawling on a textbook under a bright light, photo", "bug_book_photo"),
]

#%%
tsteps = 51
for prompt, dirname in prompt_dir_pair:
    for seed in range(100, 125):
        # prompt = "a portrait of an aristocrat"
        image, latents_traj, residue_traj, noise_uncond_traj, noise_text_traj = SD_sampler(pipe, prompt,
                   num_inference_steps=tsteps, generator=torch.cuda.manual_seed(seed))
        #%%
        savedir = join(saveroot, f"{dirname}-seed{seed}")
        os.makedirs(savedir, exist_ok=True)
        image[0].save(join(savedir, "sample.png"))
        torch.save({"latents_traj": latents_traj,
                    "residue_traj" : residue_traj,
                    "noise_uncond_traj" : noise_uncond_traj,
                    "noise_text_traj" : noise_text_traj,
                    }, join(savedir, "latents_noise_trajs.pt"))
        json.dump({"prompt": prompt, "tsteps": tsteps, "seed": seed}, open(join(savedir, "prompt.json"), "w"))

        #%%
        t_traj = pipe.scheduler.timesteps.cpu()
        alphacum_traj = pipe.scheduler.alphas_cumprod[t_traj]
        pred_z0 = (latents_traj[:-1] -
                   residue_traj * (1 - alphacum_traj).sqrt().view(-1, 1, 1, 1)) / \
                  alphacum_traj.sqrt().view(-1, 1, 1, 1)
        img_traj = latents_to_image(pred_z0[:, 0].half().to('cuda'), pipe, batch_size=11)
        save_imgrid(img_traj, join(savedir, "proj_z0_vae_decode.png"), nrow=10, )
        #%%
        mean_fin = latents_traj[-1].mean()
        std_fin = latents_traj[-1].std()
        for lag in [1,2,3,4,5,10]:
            print(f"lag {lag}")
            latent_diff = latents_traj[lag:] - latents_traj[:-lag]
            latent_renorm = denorm_sample_renorm(latent_diff[:, 0], mean_fin, std_fin)
            latdif_traj = latents_to_image(latent_renorm[:], pipe)
            save_imgrid(latdif_traj, join(savedir, f"latent_diff_lag{lag}_stdnorm_vae_decode.png"), nrow=10, )

        #%%
        """ Correlogram of the latent state difference """
        diff_cosine_mat_analysis(latents_traj, savedir, lags=(1,2,3,4,5,10))
        """Geometry of the trajectory in 2d projection"""
        trajectory_geometry_pipeline(latents_traj, savedir)
        visualize_traj_2d_cycle(latents_traj, pipe, savedir)
        """PCA analysis of the latent state / difference"""
        expvar, U, D, V = latent_PCA_analysis(latents_traj, savedir,)
        expvar_diff, U_diff, D_diff, V_diff = latent_diff_PCA_analysis(latents_traj, savedir,
                                   proj_planes=[(i, j) for i in range(8) for j in range(i+1, 8)])
        ldm_PCA_data_visualize(latents_traj, pipe, U, D, V, savedir, topcurv_num=8, topImg_num=8, prefix="latent_traj")
        ldm_PCA_data_visualize(latents_traj, pipe, U_diff, D_diff, V_diff, savedir, topcurv_num=8, topImg_num=8, prefix="latent_diff")
        torch.save({"expvar": expvar, "U": U, "D": D, "V": V}, join(savedir, "latent_PCA.pt"))
        torch.save({"expvar_diff": expvar_diff, "U_diff": U_diff, "D_diff": D_diff, "V_diff": V_diff}, join(savedir, "latent_diff_PCA.pt"))

        expvar_noise, U_noise, D_noise, V_noise = latent_PCA_analysis(residue_traj, savedir,
                                   proj_planes=[(i, j) for i in range(5) for j in range(i+1, 5)], savestr="noise_pred_traj")
        ldm_PCA_data_visualize(latents_traj, pipe, U, D, V, savedir, topcurv_num=8, topImg_num=8, prefix="noise_pred_traj")
        torch.save({"expvar": expvar_noise, "U": U_noise, "D": D_noise, "V": V_noise}, join(savedir, "noise_pred_PCA.pt"))


        expvar_noise, U_noise, D_noise, V_noise = latent_PCA_analysis(noise_uncond_traj, savedir,
                                   proj_planes=[(i, j) for i in range(5) for j in range(i+1, 5)], savestr="noise_uncond_traj")
        ldm_PCA_data_visualize(noise_uncond_traj, pipe, U, D, V, savedir, topcurv_num=8, topImg_num=8, prefix="noise_uncond_traj")
        torch.save({"expvar": expvar_noise, "U": U_noise, "D": D_noise, "V": V_noise}, join(savedir, "noise_uncond_PCA.pt"))

        expvar_noise, U_noise, D_noise, V_noise = latent_PCA_analysis(noise_text_traj, savedir,
                                   proj_planes=[(i, j) for i in range(5) for j in range(i+1, 5)], savestr="noise_text_traj")
        ldm_PCA_data_visualize(noise_text_traj, pipe, U, D, V, savedir, topcurv_num=8, topImg_num=8, prefix="noise_text_traj")
        torch.save({"expvar": expvar_noise, "U": U_noise, "D": D_noise, "V": V_noise}, join(savedir, "noise_text_PCA.pt"))
        plt.close("all")
    #     break
    # break
