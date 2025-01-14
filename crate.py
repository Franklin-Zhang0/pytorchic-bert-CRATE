import torch
from torch import nn

from einops import rearrange, repeat
from einops.layers.torch import Rearrange
import torch.nn.functional as F
import torch.nn.init as init

def pair(t):
    return t if isinstance(t, tuple) else (t, t)

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn
    def forward(self, x, mask=None, **kwargs):
        if mask is not None:
            return self.fn(self.norm(x), mask, **kwargs)
        else:
            return self.fn(self.norm(x), **kwargs)

class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout = 0., step_size=0.1):
        super().__init__()
        self.weight = nn.Parameter(torch.Tensor(dim, dim))
        with torch.no_grad():
            init.kaiming_uniform_(self.weight)
        self.step_size = step_size
        self.lambd = 0.1

    def forward(self, x):
        # compute D^T * D * x
        x1 = F.linear(x, self.weight, bias=None)
        grad_1 = F.linear(x1, self.weight.t(), bias=None)
        # compute D^T * x
        grad_2 = F.linear(x, self.weight.t(), bias=None)
        # compute negative gradient update: step_size * (D^T * x - D^T * D * x)
        grad_update = self.step_size * (grad_2 - grad_1) - self.step_size * self.lambd

        output = F.relu(x + grad_update)
        return output

class Attention(nn.Module):
    def __init__(self, dim, heads = 8, dim_head = 64, dropout = 0.):
        super().__init__()
        inner_dim = dim_head *  heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim = -1)
        self.dropout = nn.Dropout(dropout)

        self.qkv = nn.Linear(dim, inner_dim, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

    def forward(self, x, mask):
        w = rearrange(self.qkv(x), 'b n (h d) -> b h n d', h = self.heads)

        dots = torch.matmul(w, w.transpose(-1, -2)) * self.scale

        attn = self.attend(dots)
        attn = self.dropout(attn)
        if mask is not None:
            mask = mask[:, None, None, :].float()
            attn = attn * (1.0 - mask)

        out = torch.matmul(attn, w)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)
    

class Embeddings(nn.Module):
    "The embedding module from word, position and token_type embeddings."
    def __init__(self, cfg):
        super().__init__()
        self.tok_embed = nn.Embedding(cfg.vocab_size, cfg.dim) # token embedding
        self.pos_embed = nn.Embedding(cfg.max_len, cfg.dim) # position embedding
        self.seg_embed = nn.Embedding(cfg.n_segments, cfg.dim) # segment(token type) embedding

        self.norm = nn.LayerNorm(cfg.dim)
        self.drop = nn.Dropout(cfg.p_drop_hidden)

    def forward(self, x, seg):
        seq_len = x.size(1)
        pos = torch.arange(seq_len, dtype=torch.long, device=x.device)
        pos = pos.unsqueeze(0).expand_as(x) # (S,) -> (B, S)

        e = self.tok_embed(x) + self.pos_embed(pos) + self.seg_embed(seg)
        return self.drop(self.norm(e))

import torch.nn.functional as F
class Transformer(nn.Module):
    def __init__(self, cfg, ista=0.1):
        super().__init__()
        dim=cfg.dim 
        depth = cfg.n_layers 
        heads = cfg.n_heads
        dim_head = cfg.dim//cfg.n_heads
        dropout = cfg.p_drop_attn
        self.layers = nn.ModuleList([])
        # self.heads = heads
        # self.depth = depth
        # self.dim = dim
        self.embed = Embeddings(cfg)
        for _ in range(depth):
            self.layers.append(nn.ModuleList([
                PreNorm(dim, Attention(dim, heads = heads, dim_head = dim_head, dropout = dropout)),
                PreNorm(dim, FeedForward(dim, dim, dropout = dropout, step_size=ista))
            ]))

    def forward(self, x, seg, mask):
        x = self.embed(x, seg)
        depth = 0

        for attn, ff in self.layers:
            grad_x = attn(x, mask) + x

            x = ff(grad_x)
        return x

class CRATE(nn.Module):
    def __init__(self, *, image_size, patch_size, num_classes, dim, depth, heads, pool = 'cls', channels = 3, dim_head = 64, dropout = 0., emb_dropout = 0., ista=0.1):
        super().__init__()
        image_height, image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        assert image_height % patch_height == 0 and image_width % patch_width == 0, 'Image dimensions must be divisible by the patch size.'

        num_patches = (image_height // patch_height) * (image_width // patch_width)
        patch_dim = channels * patch_height * patch_width
        assert pool in {'cls', 'mean'}, 'pool type must be either cls (cls token) or mean (mean pooling)'

        self.to_patch_embedding = nn.Sequential(
            Rearrange('b c (h p1) (w p2) -> b (h w) (p1 p2 c)', p1 = patch_height, p2 = patch_width),
            nn.LayerNorm(patch_dim),
            nn.Linear(patch_dim, dim),
            nn.LayerNorm(dim),
        )

        self.pos_embedding = nn.Parameter(torch.randn(1, num_patches + 1, dim))
        self.cls_token = nn.Parameter(torch.randn(1, 1, dim))
        self.dropout = nn.Dropout(emb_dropout)

        self.transformer = Transformer(dim, depth, heads, dim_head, dropout, ista=ista)

        self.pool = pool
        self.to_latent = nn.Identity()

        self.mlp_head = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, num_classes)
        )

    def forward(self, img):
        x = self.to_patch_embedding(img)
        b, n, _ = x.shape

        cls_tokens = repeat(self.cls_token, '1 1 d -> b 1 d', b = b)
        x = torch.cat((cls_tokens, x), dim=1)
        x += self.pos_embedding[:, :(n + 1)]
        x = self.dropout(x)

        x = self.transformer(x)
        feature_pre = x
        x = x.mean(dim = 1) if self.pool == 'mean' else x[:, 0]

        x = self.to_latent(x)
        feature_last = x
        return self.mlp_head(x)


def CRATE_tiny(num_classes = 1000):
    return CRATE(image_size=224,
                    patch_size=16,
                    num_classes=num_classes,
                    dim=384,
                    depth=12,
                    heads=6,
                    dropout=0.0,
                    emb_dropout=0.0,
                    dim_head=384//6)

def CRATE_small(num_classes = 1000):
    return CRATE(image_size=224,
                    patch_size=16,
                    num_classes=num_classes,
                    dim=576,
                    depth=12,
                    heads=12,
                    dropout=0.0,
                    emb_dropout=0.0,
                    dim_head=576//12)

def CRATE_base(num_classes = 1000):
    return CRATE(image_size=224,
                patch_size=16,
                num_classes=num_classes,
                dim=768,
                depth=12,
                heads=12,
                dropout=0.0,
                emb_dropout=0.0,
                dim_head=768//12)

def CRATE_large(num_classes = 1000):
    return CRATE(image_size=224,
                patch_size=16,
                num_classes=num_classes,
                dim=1024,
                depth=24,
                heads=16,
                dropout=0.0,
                emb_dropout=0.0,
                dim_head=1024//16)