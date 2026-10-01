"""Tiny multilingual embedder (CPU) used for candidate recall."""
import os, torch
from transformers import AutoTokenizer, AutoModel
torch.set_num_threads(int(os.environ.get("EMBED_THREADS", "8")))

class Embedder:
    def __init__(self, repo="intfloat/multilingual-e5-small"):
        self.repo = repo
        self.tok = AutoTokenizer.from_pretrained(repo)
        self.model = AutoModel.from_pretrained(repo).eval()
        self.e5 = "e5" in repo
    @torch.inference_mode()
    def encode(self, texts, kind="passage"):
        if self.e5:
            texts = [f"{kind}: {t}" for t in texts]
        b = self.tok(texts, padding=True, truncation=True, max_length=96, return_tensors="pt")
        h = self.model(**b).last_hidden_state
        if self.e5:
            m = b["attention_mask"].unsqueeze(-1).float()
            v = (h * m).sum(1) / m.sum(1)
        else:
            v = h[:, 0]
        return torch.nn.functional.normalize(v, dim=-1)
    def scores(self, query, docs):
        q = self.encode([query], "query")
        d = self.encode(docs, "passage")
        return (d @ q.T).squeeze(-1).tolist()
