from typing import Optional, Tuple
from diffusers.configuration_utils import ConfigMixin, register_to_config
from diffusers.models.modeling_utils import ModelMixin
from diffusers.models.unets.unet_2d_blocks import UNetMidBlock2D, get_down_block, get_up_block
#from diffusers.models.unet_2d_blocks import UNetMidBlock2D, get_down_block, get_up_block
import torch.nn as nn
import torch
import numpy as np
from PIL import Image
from tqdm import tqdm


def zero_module(module):
    """
    Zero out the parameters of a module and return it.
    """
    for p in module.parameters():
        p.detach().zero_()
    return module


import torch
import numpy as np


class ImageRenderer:
    def __init__(self, xl_base_patched):
        self.xl_base_patched = xl_base_patched

    def render_diffuse_image(self, input_image_pil, samples, decoded_latent):
        pixel = decoded_latent[0].permute(2, 0, 1).unsqueeze(0)

        ## Decoder requires dimension to be 64-aligned.
        B, C, H, W = pixel.shape
        assert H % 64 == 0, f"Height({H}) is not multiple of 64."
        assert W % 64 == 0, f"Height({W}) is not multiple of 64."

        decoded = []
        sub_batch_size = 1
        for start_idx in range(0, samples.shape[0], sub_batch_size):
            decoded.append(
                self.xl_base_patched.tvae.decode_pixel(
                    pixel[start_idx : start_idx + sub_batch_size],
                    samples[start_idx : start_idx + sub_batch_size],
                )
            )
        pixel_with_alpha = torch.cat(decoded, dim=0)

        # [B, C, H, W] => [B, H, W, C]
        pixel_with_alpha = pixel_with_alpha.movedim(1, -1)
        image = pixel_with_alpha[..., 1:]
        alpha = pixel_with_alpha[..., 0]

        i = np.clip(255.0 * image[0].cpu().numpy(), 0, 255).astype(np.uint8)
        i = np.squeeze(i)
        a = np.clip(255.0 * alpha[0].cpu().numpy(), 0, 255).astype(np.uint8)
        a = np.squeeze(a)
        img = Image.fromarray(i).convert("RGBA")
        alpha = Image.fromarray(a).convert("L")
        img.putalpha(alpha)

        w, h = img.size
        if isinstance(input_image_pil, Image.Image):
            bgimg = input_image_pil.resize((w, h), resample=Image.LANCZOS)
            img = Image.composite(img, bgimg, alpha)
            preview = None
        else:
            pxlsize = 32
            preview = Image.new("RGBA", (int(w / pxlsize), int(h / pxlsize)))
            for i in range(int(w / pxlsize)):
                for j in range(int(h / pxlsize)):
                    if (i + j) % 2:
                        preview.putpixel((i, j), (96, 96, 96))
                    else:
                        preview.putpixel((i, j), (128, 128, 128))
            preview = preview.resize((w, h), resample=Image.NEAREST)
            preview = Image.composite(img, preview, alpha)
        return preview, img


# From https://github.com/huchenlei/ComfyUI-layerdiffuse/blob/main/lib_layerdiffusion/models.py#L61
# 1024 * 1024 * 3 -> 16 * 16 * 512 -> 1024 * 1024 * 3
class UNet1024(ModelMixin, ConfigMixin):
    @register_to_config
    def __init__(
        self,
        in_channels: int = 3,
        out_channels: int = 3,
        down_block_types: Tuple[str] = (
            "DownBlock2D",
            "DownBlock2D",
            "DownBlock2D",
            "DownBlock2D",
            "AttnDownBlock2D",
            "AttnDownBlock2D",
            "AttnDownBlock2D",
        ),
        up_block_types: Tuple[str] = (
            "AttnUpBlock2D",
            "AttnUpBlock2D",
            "AttnUpBlock2D",
            "UpBlock2D",
            "UpBlock2D",
            "UpBlock2D",
            "UpBlock2D",
        ),
        block_out_channels: Tuple[int] = (32, 32, 64, 128, 256, 512, 512),
        layers_per_block: int = 2,
        mid_block_scale_factor: float = 1,
        downsample_padding: int = 1,
        downsample_type: str = "conv",
        upsample_type: str = "conv",
        dropout: float = 0.0,
        act_fn: str = "silu",
        attention_head_dim: Optional[int] = 8,
        norm_num_groups: int = 4,
        norm_eps: float = 1e-5,
    ):
        super().__init__()

        # input
        self.conv_in = nn.Conv2d(
            in_channels, block_out_channels[0], kernel_size=3, padding=(1, 1)
        )
        self.latent_conv_in = zero_module(
            nn.Conv2d(4, block_out_channels[2], kernel_size=1)
        )

        self.down_blocks = nn.ModuleList([])
        self.mid_block = None
        self.up_blocks = nn.ModuleList([])

        # down
        output_channel = block_out_channels[0]
        for i, down_block_type in enumerate(down_block_types):
            input_channel = output_channel
            output_channel = block_out_channels[i]
            is_final_block = i == len(block_out_channels) - 1

            down_block = get_down_block(
                down_block_type,
                num_layers=layers_per_block,
                in_channels=input_channel,
                out_channels=output_channel,
                temb_channels=None,
                add_downsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                attention_head_dim=(
                    attention_head_dim
                    if attention_head_dim is not None
                    else output_channel
                ),
                downsample_padding=downsample_padding,
                resnet_time_scale_shift="default",
                downsample_type=downsample_type,
                dropout=dropout,
            )
            self.down_blocks.append(down_block)

        # mid
        self.mid_block = UNetMidBlock2D(
            in_channels=block_out_channels[-1],
            temb_channels=None,
            dropout=dropout,
            resnet_eps=norm_eps,
            resnet_act_fn=act_fn,
            output_scale_factor=mid_block_scale_factor,
            resnet_time_scale_shift="default",
            attention_head_dim=(
                attention_head_dim
                if attention_head_dim is not None
                else block_out_channels[-1]
            ),
            resnet_groups=norm_num_groups,
            attn_groups=None,
            add_attention=True,
        )

        # up
        reversed_block_out_channels = list(reversed(block_out_channels))
        output_channel = reversed_block_out_channels[0]
        for i, up_block_type in enumerate(up_block_types):
            prev_output_channel = output_channel
            output_channel = reversed_block_out_channels[i]
            input_channel = reversed_block_out_channels[
                min(i + 1, len(block_out_channels) - 1)
            ]

            is_final_block = i == len(block_out_channels) - 1

            up_block = get_up_block(
                up_block_type,
                num_layers=layers_per_block + 1,
                in_channels=input_channel,
                out_channels=output_channel,
                prev_output_channel=prev_output_channel,
                temb_channels=None,
                add_upsample=not is_final_block,
                resnet_eps=norm_eps,
                resnet_act_fn=act_fn,
                resnet_groups=norm_num_groups,
                attention_head_dim=(
                    attention_head_dim
                    if attention_head_dim is not None
                    else output_channel
                ),
                resnet_time_scale_shift="default",
                upsample_type=upsample_type,
                dropout=dropout,
            )
            self.up_blocks.append(up_block)
            prev_output_channel = output_channel

        # out
        self.conv_norm_out = nn.GroupNorm(
            num_channels=block_out_channels[0], num_groups=norm_num_groups, eps=norm_eps
        )
        self.conv_act = nn.SiLU()
        self.conv_out = nn.Conv2d(
            block_out_channels[0], out_channels, kernel_size=3, padding=1
        )

    def forward(self, x, latent):
        sample_latent = self.latent_conv_in(latent)
        sample = self.conv_in(x)
        emb = None

        down_block_res_samples = (sample,)
        for i, downsample_block in enumerate(self.down_blocks):
            if i == 3:
                sample = sample + sample_latent

            sample, res_samples = downsample_block(hidden_states=sample, temb=emb)
            down_block_res_samples += res_samples

        sample = self.mid_block(sample, emb)

        for upsample_block in self.up_blocks:
            res_samples = down_block_res_samples[-len(upsample_block.resnets) :]
            down_block_res_samples = down_block_res_samples[
                : -len(upsample_block.resnets)
            ]
            sample = upsample_block(sample, res_samples, emb)

        sample = self.conv_norm_out(sample)
        sample = self.conv_act(sample)
        sample = self.conv_out(sample)
        return sample


# From https://github.com/huchenlei/ComfyUI-layerdiffuse/blob/main/lib_layerdiffusion/models.py#L248
class TransparentVAEDecoder:
    def __init__(self, sd, device, dtype):
        self.load_device = device
        self.dtype = dtype

        model = UNet1024(in_channels=3, out_channels=4)
        model.load_state_dict(sd, strict=True)
        model.to(self.load_device, dtype=self.dtype)
        model.eval()
        self.model = model

    @torch.no_grad()
    def estimate_single_pass(self, pixel, latent):
        y = self.model(pixel, latent)
        return y

    @torch.no_grad()
    def estimate_augmented(self, pixel, latent):
        args = [
            [False, 0],
            [False, 1],
            [False, 2],
            [False, 3],
            [True, 0],
            [True, 1],
            [True, 2],
            [True, 3],
        ]

        result = []

        for flip, rok in tqdm(args):
            feed_pixel = pixel.clone()
            feed_latent = latent.clone()

            if flip:
                feed_pixel = torch.flip(feed_pixel, dims=(3,))
                feed_latent = torch.flip(feed_latent, dims=(3,))

            feed_pixel = torch.rot90(feed_pixel, k=rok, dims=(2, 3))
            feed_latent = torch.rot90(feed_latent, k=rok, dims=(2, 3))

            eps = self.estimate_single_pass(feed_pixel, feed_latent).clip(0, 1)
            eps = torch.rot90(eps, k=-rok, dims=(2, 3))

            if flip:
                eps = torch.flip(eps, dims=(3,))

            result += [eps]

        result = torch.stack(result, dim=0)
        median = torch.median(result, dim=0).values
        return median

    @torch.no_grad()
    def decode_pixel(
        self, pixel: torch.TensorType, latent: torch.TensorType
    ) -> torch.TensorType:
        # pixel.shape = [B, C=3, H, W]
        assert pixel.shape[1] == 3
        pixel_device = pixel.device
        pixel_dtype = pixel.dtype

        pixel = pixel.to(device=self.load_device, dtype=self.dtype)
        latent = latent.to(device=self.load_device, dtype=self.dtype)
        # y.shape = [B, C=4, H, W]
        y = self.estimate_augmented(pixel, latent)
        y = y.clip(0, 1)
        assert y.shape[1] == 4
        # Restore image to original device of input image.
        return y.to(pixel_device, dtype=pixel_dtype)
