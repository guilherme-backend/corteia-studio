"""Language-model bridge: real context analysis with an auditable rubric.

Three providers are supported through one code path:

* ``openai``   — any OpenAI-compatible ``/chat/completions`` endpoint
                 (OpenAI itself, Groq, OpenRouter, Together, local LM Studio…)
* ``ollama``   — ``http://localhost:11434/v1`` (same protocol, no key)
* ``off``      — no network at all: the local feature model in
                 :mod:`corteia.core.analyze` still scores everything, and the UI
                 says plainly that no LLM was used.

The model never returns numbers we cannot trace: each candidate is sent with an
id, and the response must be a JSON array of ``{id, score, title, why}``.
Malformed or partial answers are validated, clamped and reported — a failed
call falls back to the local score instead of fabricating one.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import get_config
from ..models import Transcript
from ..paths import temp_file

log = logging.getLogger("corteia.llm")

RUBRIC = """Você é um editor-chefe de cortes verticais (TikTok, Reels, Shorts).
Analise os trechos abaixo de uma transcrição e avalie CADA um individualmente.

Critérios (o que torna um corte bom):
1. Funciona sozinho: tem começo, desenvolvimento e conclusão, sem depender do que veio antes.
2. Não corta frase no meio; a última palavra fecha a ideia.
3. Conteúdo de alto engajamento: engraçado, emocionante, surpreendente, polêmico,
   uma opinião forte, uma história bem contada, um dado concreto, uma frase de impacto.
4. Gancho nos primeiros 3 segundos (algo que faz a pessoa parar de rolar).
5. Evita: saudações, propaganda, conversa de bastidor, repetição, listas de processos,
   silêncio, trechos que só fazem sentido para quem viu o vídeo inteiro.

Dê score de 0.0 a 1.0 (não use nota de participação: 0.2 é ruim, 0.5 é ok,
0.8 é muito bom, 0.95 é corte imbatível). Seja rigoroso e distribua as notas.
Escreva title em português do Brasil: até 60 caracteres, sem aspas, específico,
sem clickbait vazio, sem emoji. "why" em até 25 palavras, citando o trecho que
justifica."""

SYSTEM = "Você responde exclusivamente JSON válido, sem markdown, sem comentários."

_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "score": {"type": "number"},
            "title": {"type": "string"},
            "why": {"type": "string"},
        },
        "required": ["id", "score", "title", "why"],
        "additionalProperties": False,
    },
}


@dataclass(slots=True)
class LLMStatus:
    provider: str
    model: str
    base_url: str
    usable: bool
    reason: str = ""

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url,
                "usable": self.usable, "reason": self.reason}


class LLMClient:
    """Blocking client. Call it from a worker thread, never from the GUI thread."""

    def __init__(self, config=None) -> None:
        self.cfg = config or get_config()
        self._lock = threading.Lock()
        self.last_error: str = ""

    # ------------------------------------------------------------- status
    def status(self) -> LLMStatus:
        provider = (self.cfg.str_("api.provider") or "off").strip().lower()
        model = self.cfg.str_("api.model")
        base = (self.cfg.str_("api.base_url") or "").strip() or _default_base(provider)
        if provider in ("", "off", "none", "local"):
            return LLMStatus("off", model, base, False, "nenhum provedor selecionado")
        if not model:
            return LLMStatus(provider, model, base, False, "modelo não informado")
        if provider != "ollama" and not self.cfg.str_("api.key"):
            return LLMStatus(provider, model, base, False, "chave de API não informada")
        return LLMStatus(provider, model, base, True)

    def usable(self) -> bool:
        return self.status().usable

    # ------------------------------------------------------------- transport
    def chat(self, messages: list[dict], *, json_mode: bool = False,
             temperature: float | None = None, max_tokens: int | None = None,
             cancel: threading.Event | None = None) -> str:
        st = self.status()
        if not st.usable:
            raise RuntimeError(st.reason)
        url = _join(st.base_url, "chat/completions")
        payload: dict = {
            "model": st.model,
            "messages": messages,
            "temperature": float(self.cfg.float_("api.temperature")
                                 if temperature is None else temperature),
            "max_tokens": int(max_tokens or self.cfg.int_("api.max_output_tokens") or 4000),
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if st.provider != "ollama":
            headers["Authorization"] = f"Bearer {self.cfg.str_('api.key')}"
        elif not st.model.endswith(".gguf"):
            payload["model"] = st.model
        timeout = float(self.cfg.int_("api.timeout") or 60)
        body = self._post(url, payload, headers, timeout=timeout, cancel=cancel)
        try:
            data = json.loads(body)
            choices = data.get("choices") or []
            if not choices:
                raise ValueError("resposta sem 'choices'")
            content = (choices[0].get("message") or {}).get("content") or ""
            if not content and choices[0].get("text"):
                content = choices[0]["text"]
            return _strip_fence(content)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            snippet = (body or "")[:400].replace("\n", " ")
            raise RuntimeError(f"resposta do modelo ilegível ({exc}): {snippet}") from exc

    def _post(self, url: str, payload: dict, headers: dict, *, timeout: float,
              cancel: threading.Event | None = None) -> str:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        ctx = ssl.create_default_context()
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:                              # noqa: BLE001
                pass
            msg = _http_meaning(exc.code, detail, self.status())
            self.last_error = msg
            raise RuntimeError(msg) from exc
        except urllib.error.URLError as exc:
            msg = f"não foi possível falar com {url}: {exc.reason}"
            self.last_error = msg
            raise RuntimeError(msg) from exc

    # ------------------------------------------------------------- tasks
    def judge(self, items: list[tuple[str, str, float]], *, progress=None,
              cancel: threading.Event | None = None,
              chunk_chars: int = 9000) -> dict[str, float]:
        """Score ``[(id, text, duration)]`` → ``{id: 0..1}``.

        Titles and reasons are also captured (``id:title`` / ``id:why``) so the
        UI can show what the model wrote instead of a machine-generated
        sentence.  Large videos are split into chunks that each stay inside the
        context window; a failing chunk is skipped, not faked.
        """
        out: dict[str, float] = {}
        if not items:
            return out
        batches = _batch(items, chunk_chars)
        for bi, batch in enumerate(batches, start=1):
            if cancel is not None and cancel.is_set():
                break
            if progress:
                progress(6.0 * bi / len(batches),
                         f"avaliando com o modelo ({bi}/{len(batches)})")
            prompt = self._judge_prompt(batch)
            try:
                raw = self.chat([{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": prompt}],
                                json_mode=True, cancel=cancel)
                rows = _parse_rows(raw, {i for i, _, _ in batch})
            except Exception as exc:                       # noqa: BLE001
                self.last_error = str(exc)
                log.warning("chunk %d/%d de avaliação falhou: %s", bi, len(batches), exc)
                continue
            for r in rows:
                _ingest(out, r)
        return out

    def _judge_prompt(self, batch: list[tuple[str, str, float]]) -> str:
        lines = [RUBRIC, "", "Responda: {\"results\": [ ... ]} onde cada item é",
                 json.dumps(_SCHEMA["items"], ensure_ascii=False),
                 "", "TRECHOS (id | duração | texto):"]
        for cid, text, dur in batch:
            clean = re.sub(r"\s+", " ", text).strip()
            lines.append(f'--- id="{cid}" {dur:.0f}s: {clean[:1400]}')
        lines.append("")
        lines.append('Devolva TODOS os ids listados. Exato formato: {"results": [...]}')
        return "\n".join(lines)

    def titles(self, items: list[tuple[str, str]], *, cancel=None) -> dict[str, str]:
        """Short viral titles for ``[(id, text)]`` — used when only titles are wanted."""
        if not items:
            return {}
        prompt = ("Dê títulos curtos (até 60 caracteres, português do Brasil, específicos, "
                  "sem emoji, sem aspas) para cada trecho.\nResponda JSON "
                  '{"results":[{"id":"...","title":"..."}]}.\n\n')
        prompt += "\n".join(f'id="{i}": {re.sub(chr(92) + "s+", " ", t)[:900]}' for i, t in items)
        try:
            raw = self.chat([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt}], json_mode=True, cancel=cancel)
            rows = _parse_rows(raw, {i for i, _ in items})
            return {str(r["id"]): str(r.get("title", ""))[:70] for r in rows if r.get("title")}
        except Exception as exc:                             # noqa: BLE001
            self.last_error = str(exc)
            return {}

    def topics(self, transcript: Transcript, duration: float, *, cancel=None,
                max_points: int = 14) -> list[float]:
        """Time stamps where the subject changes, asked straight from the model."""
        if not transcript.segments:
            return []
        compact = "\n".join(f"[{s.start:6.1f}] {re.sub(chr(92) + 's+', ' ', s.text)[:160]}"
                            for s in transcript.segments)[:12000]
        prompt = (
            "A transcrição abaixo tem marcações de tempo em segundos.\n"
            "Liste os segundos em que o ASSUNTO muda de fato (não marque cada frase).\n"
            f"No máximo {max_points} fronteiras, para um vídeo de {duration:.0f} s.\n"
            'Responda JSON: {"boundaries": [12.4, 87.0, ...]}\n\n' + compact)
        try:
            raw = self.chat([{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": prompt}], json_mode=True, cancel=cancel)
            data = json.loads(raw)
            pts = data.get("boundaries") or data.get("results") or []
            out = []
            for p in pts:
                v = p.get("t") if isinstance(p, dict) else p
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if 1.0 < f < max(2.0, duration - 1.0):
                    out.append(round(f, 1))
            return sorted(set(out))[:max_points]
        except Exception as exc:                               # noqa: BLE001
            self.last_error = str(exc)
            return []

    def test_connection(self) -> tuple[bool, str]:
        """Used by the Settings page button. Never guesses: it really calls."""
        st = self.status()
        if not st.usable:
            return False, f"Configuração incompleta: {st.reason}"
        try:
            out = self.chat([{"role": "user", "content": "Responda apenas: OK"}],
                            max_tokens=8, temperature=0.0)
            return True, f"{st.provider} · {st.model} respondeu: {out.strip()[:40]!r}"
        except Exception as exc:                                 # noqa: BLE001
            return False, str(exc)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _default_base(provider: str) -> str:
    p = (provider or "").lower()
    if p == "ollama":
        return "http://localhost:11434/v1"
    if p == "openai":
        return "https://api.openai.com/v1"
    return ""


def _join(base: str, tail: str) -> str:
    base = (base or "").rstrip("/")
    if not base:
        raise RuntimeError("URL base da API não configurada")
    if not base.endswith("/v1") and "/v1" not in base and "api.openai.com" in base:
        base += "/v1"
    return f"{base}/{tail.lstrip('/')}"


def _strip_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _batch(items: list[tuple[str, str, float]], chunk_chars: int) -> list[list[tuple[str, str, float]]]:
    """Group items so each prompt stays inside the model's context window."""
    batches: list[list[tuple[str, str, float]]] = []
    cur: list[tuple[str, str, float]] = []
    size = 0
    for it in items:
        cost = min(len(it[1]), 1400) + 40
        if size + cost > chunk_chars and cur:
            batches.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += cost
    if cur:
        batches.append(cur)
    return batches


def _parse_rows(raw: str, known_ids: set[str]) -> list[dict]:
    """Validate the model answer, keeping only rows whose id really exists."""
    try:
        data = json.loads(raw)
    except ValueError:
        m = re.search(r"\[[\s\S]*\]", raw or "")
        if not m:
            raise ValueError("nenhum JSON na resposta")
        data = json.loads(m.group(0))
    if isinstance(data, dict):
        for key in ("results", "items", "clips", "scores", "boundaries"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("JSON raiz não é lista")
    rows: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id", "")).strip().strip('"')
        if cid and cid in known_ids:
            rows.append(item)
        elif not cid:
            continue
    if not rows:
        raise ValueError("a resposta não continha nenhum id válido")
    return rows


def _ingest(out: dict[str, float], row: dict) -> None:
    cid = str(row["id"])
    try:
        score = float(row.get("score", row.get("rating", row.get("value", 0.0))))
    except (TypeError, ValueError):
        score = 0.0
    if score > 1.5:                       # models love 0..10 or 0..100 scales
        score = score / 100.0 if score > 20 else score / 10.0
    out[cid] = max(0.0, min(1.0, score))
    title = re.sub(r"\s+", " ", str(row.get("title", ""))).strip().strip('"“”')
    if title:
        out[f"{cid}:title"] = title[:70]
    why = re.sub(r"\s+", " ", str(row.get("why", row.get("reason", "")))).strip()
    if why:
        out[f"{cid}:why"] = why[:160]
    desc = re.sub(r"\s+", " ", str(row.get("description", ""))).strip()
    if desc:
        out[f"{cid}:desc"] = desc[:220]


def _http_meaning(code: int, detail: str, st: LLMStatus) -> str:
    d = (detail or "").lower()
    if code == 401:
        return ("a chave de API foi recusada (401). Confira Configurações → APIs externas; "
                "para modelos locais use Ollama, que não exige chave.")
    if code == 403:
        return f"acesso negado (403) em {st.model}. Verifique se o modelo está habilitado na conta."
    if code == 404:
        hint = "o modelo não existe nesse endpoint"
        if "groq" in d or "openrouter" in d:
            hint += " (confira o nome exato no painel do provedor)"
        return f"{hint} (404): {st.model}"
    if code in (429,):
        return ("o provedor limitou as requisições (429). Reduza o número de cortes ou "
                "tente novamente em instantes.")
    if code >= 500:
        return f"o provedor está com falha ({code}). O app usou a pontuação local neste lote."
    return f"erro {code} do provedor {st.provider}: {(detail or '')[:220]}"


_client: LLMClient | None = None


def get_client(config=None) -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient(config)
    return _client
