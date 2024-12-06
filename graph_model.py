import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.data import Batch

class GraphormerMultiHeadAttention(nn.Module):
    def __init__(self, embed_dim, num_heads, dropout=0.1):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = embed_dim // num_heads
        assert (self.head_dim * num_heads == embed_dim), "embed_dim must be divisible by num_heads"

        self.q_proj = nn.Linear(embed_dim, embed_dim)
        self.k_proj = nn.Linear(embed_dim, embed_dim)
        self.v_proj = nn.Linear(embed_dim, embed_dim)
        self.out_proj = nn.Linear(embed_dim, embed_dim)
        self.attn_drop = nn.Dropout(dropout)

    def forward(self, x):
        # x: [B, N, D]
        B, N, D = x.shape
        Q = self.q_proj(x) 
        K = self.k_proj(x) 
        V = self.v_proj(x) 

        Q = Q.reshape(B, N, self.num_heads, self.head_dim).transpose(1, 2) # [B, h, N, d]
        K = K.reshape(B, N, self.num_heads, self.head_dim).transpose(1, 2)
        V = V.reshape(B, N, self.num_heads, self.head_dim).transpose(1, 2)

        scores = (Q @ K.transpose(-1, -2)) / (self.head_dim ** 0.5) # [B, h, N, N]
        attn = F.softmax(scores, dim=-1) # [B, h, N, N]
        attn = self.attn_drop(attn)
        out = attn @ V # [B, h, N, d]
        out = out.transpose(1, 2).reshape(B, N, D) # [B, N, D]
        out = self.out_proj(out)
        return out

class GraphormerLayer(nn.Module):
    def __init__(self, hidden_dim, num_heads, dropout):
        super().__init__()
        self.attn = GraphormerMultiHeadAttention(hidden_dim, num_heads, dropout=dropout)
        self.norm1 = nn.LayerNorm(hidden_dim)
        self.ffn = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim*4),
            nn.ReLU(),
            nn.Linear(hidden_dim*4, hidden_dim)
        )
        self.norm2 = nn.LayerNorm(hidden_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        h = self.norm1(x)
        h = self.attn(h)
        x = x + self.dropout(h)

        h = self.norm2(x)
        h = self.ffn(h)
        x = x + self.dropout(h)
        return x

class Graphormer(nn.Module):
    """
    仅使用节点度作为结构特征:
    - 节点特征包含: 输入特征 + 节点度嵌入 + 节点位置嵌入
    """
    def __init__(self, input_dim, hidden_dim, num_heads=4, num_layers=4, dropout=0.1, max_degree=256, max_nodes=100000):
        super(Graphormer, self).__init__()
        self.max_degree = max_degree
        self.max_nodes = max_nodes

        self.input_proj = nn.Linear(input_dim, hidden_dim)
        self.degree_embedding = nn.Embedding(self.max_degree, hidden_dim)
        self.pos_embedding = nn.Embedding(self.max_nodes, hidden_dim)

        self.layers = nn.ModuleList([
            GraphormerLayer(hidden_dim, num_heads, dropout) for _ in range(num_layers)
        ])
        self.output_norm = nn.LayerNorm(hidden_dim)

    def forward(self, x, degree, node_ids):
        # x: [N, input_dim]
        # degree: [N]
        # node_ids: [N]
        # 确保degree和node_ids不越界
        degree_clamped = torch.clamp(degree, max=self.max_degree-1)
        node_ids_clamped = torch.clamp(node_ids, max=self.max_nodes-1)

        N = x.size(0)
        h = self.input_proj(x).unsqueeze(0) # [1, N, D]
        deg_embed = self.degree_embedding(degree_clamped) # [N, D]
        h = h + deg_embed.unsqueeze(0) # [1, N, D]

        pos_embed = self.pos_embedding(node_ids_clamped) # [N, D]
        h = h + pos_embed.unsqueeze(0) # [1, N, D]

        for layer in self.layers:
            h = layer(h) # [1, N, D]

        h = self.output_norm(h) # [1, N, D]
        h = h.squeeze(0) # [N, D]
        return h

class GraphormerJEPA(nn.Module):
    def __init__(self, input_dim, hidden_dim, output_dim, max_degree=256, max_nodes=100000):
        super(GraphormerJEPA, self).__init__()
        self.context_encoder = Graphormer(input_dim, hidden_dim, max_degree=max_degree, max_nodes=max_nodes)
        self.target_encoder = Graphormer(input_dim, hidden_dim, max_degree=max_degree, max_nodes=max_nodes)
        self.prediction_head = nn.Linear(hidden_dim, output_dim)

    def forward(self, context_batch, target_batch):
        def encode(encoder, batch):
            x = batch.x
            degree = batch.degree
            node_ids = torch.arange(x.size(0), device=x.device)

            emb_list = []
            g_ptr = batch.ptr
            for i in range(len(g_ptr)-1):
                start, end = g_ptr[i], g_ptr[i+1]
                x_g = x[start:end]
                degree_g = degree[start:end]
                node_ids_g = node_ids[start:end]
                emb_g = encoder(x_g, degree_g, node_ids_g) # [N_g, D]
                graph_emb = emb_g.mean(dim=0, keepdim=True) # [1, D]
                emb_list.append(graph_emb)
            return torch.cat(emb_list, dim=0) # [num_graphs, D]

        context_embeddings = encode(self.context_encoder, context_batch) # [B, D]
        target_embeddings = encode(self.target_encoder, target_batch) # [B, D]
        predicted_embeddings = self.prediction_head(context_embeddings) # [B, D]

        return predicted_embeddings, target_embeddings
