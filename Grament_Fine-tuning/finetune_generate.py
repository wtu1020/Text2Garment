from diffusers import DiffusionPipeline
import torch
from diffusers import DDIMScheduler

# Configure the fine-tuned weight path address
pipeline = DiffusionPipeline.from_pretrained("/weights/stabilityai/stable-diffusion-2-1-base", torch_dtype=torch.float16, use_safetensors=True).to("cuda")

# Using the DDIM Scheduler
# pipeline.scheduler = DDIMScheduler.from_config(pipeline.scheduler.config)

# Generate the garment image
image = pipeline("a jumpsuit", num_inference_steps=100, guidance_scale=7.5).images[0]

# Save the generated image
image.save("./save/garment1.png")
