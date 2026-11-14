# Achados da suíte de testes (bugs reais do núcleo — nada em `corteia/` foi editado)

Suíte: `QT_QPA_PLATFORM=offscreen python -m pytest -q tests/` → **47 passed, 1 xfailed**.

Cada item abaixo foi reproduzido diretamente contra o código atual. O único caso
coberto por teste entrou como `@pytest.mark.xfail(strict=False)`.

---

## 1. Enum desconhecido no JSON derruba o projeto inteiro — **tem teste xfail**

**Onde:** `corteia/models.py:495` — `status=ClipStatus(d.get("status", "suggested"))`,
que só protege contra chave **ausente**, não contra chave **desconhecida**. O efeito vaza
em `corteia/project.py:80-87` (`ProjectStore.load`). Os outros enums têm o mesmo padrão:
`ClipEdit.aspect`, `ExportSettings.codec/resolution/aspect`,
`AnalysisOptions.aspect/subtitle_style`, `JobState` (`corteia/models.py`).

**Reprodução:**

```python
d = project.to_dict()
d["clips"][0]["status"] = "status_de_um_futuro"
Project.from_dict(d)      # ValueError: 'status_inexistente' is not a valid ClipStatus
```

Pelo `ProjectStore.load`, o `ValueError` vira `OSError("Projeto corrompido...")` e o
arquivo ainda é **renomeado para `.corrupt`** — ou seja, um app mais antigo que abriu um
projeto salvo por uma versão nova destrói o índice do usuário, em vez de ignorar o campo.
`Recent.from_dict` já faz o certo (`{k: v for k, v in d.items() if k in fields}`), então a
tolerância a campos novos é uma regra conhecida e só não foi aplicada aos enums.

**Correção sugerida:** um helper `_enum_or(Enum, value, default)` usado por todos os
`from_dict` (`Clip.status`, `ClipEdit.aspect`, `ExportSettings.codec/resolution/aspect`,
`AnalysisOptions.aspect/subtitle_style`, `JobState`). Valor inválido → default +
`log.warning`. O load deveria sobreviver a qualquer campo desconhecido; só JSON
sintaticamente inválido merece virar `.corrupt`.

---

## 2. `render.output_size()` quebra com um `aspect` diferente do configurado

**Onde:** `corteia/core/render.py:376-381` + `corteia/models.py:527` (`target_size`).

**Reprodução:**

```python
output_size(ExportSettings(), AspectRatio.SQUARE_1_1)
# AttributeError: 'str' object has no attribute 'short_side'
```

**Causa:** o clone é montado com `ES(**{**settings.to_dict(), "aspect": aspect.value})`.
`to_dict()` serializa `resolution`/`codec`/`aspect` como **strings** (`X.value`), e o
construtor do dataclass não reconverte — então `self.resolution` é `"1080p"` (um `str`) e
`str.short_side` não existe. O caminho só funciona hoje porque `render_clip` chama
`output_size(settings, clip.edit.aspect or settings.aspect)`, e na prática o `or` quase
sempre entrega `None` ou o próprio `settings.aspect`, caindo no retorno direto.

**Correção sugerida:** usar `ExportSettings.from_dict({...})` (que já reconverte enums) no
lugar do `ES(**to_dict())`, ou `replace(settings, aspect=aspect)` — que preserva os objetos
e ainda elimina o import local de `ExportSettings` dentro da função.

---

## 3. `Config.save()` não cria o diretório-pai

**Onde:** `corteia/config.py:103-107`.

**Reprodução:**

```python
Config(Path("/tmp/ainda/nao/existe/config.json")).set("ui.theme", "light")
# FileNotFoundError: .../config.tmp
```

`load()` é tolerante (arquivo ausente → defaults), mas `save()` escreve `path.with_suffix
(".tmp")` sem `mkdir(parents=True)`. Dentro do app isso passa despercebido porque
`paths.data_dir()` é chamado antes e cria a pasta — qualquer chamada com caminho próprio
(instalação portável, migração, teste) falha.

**Correção sugerida:** `self._path.parent.mkdir(parents=True, exist_ok=True)` no início de
`save()` (o mesmo padrão já usado em `ProjectStore._write`, `corteia/project.py:124-125`).

---

## 4. `media.frame_at()` aceita tempo negativo

**Onde:** `corteia/core/media.py`, `frame_at(path, at, width)`.

**Reprodução:** `frame_at(Path("/tmp/probe_src.mp4"), -5.0)` devolve um quadro
`(360, 640, 3)` em vez de `None`: o `-5` é repassado ao `-ss` do FFmpeg, que trata o valor
negativo como 0 e entrega o primeiro quadro. Uma trilha de keyframes montada a partir de um
tempo negativo silenciosamente receberia o quadro 0 como se fosse o quadro `-5 s`.

**Correção sugerida:** `if at < 0: return None` logo após o `probe`, no mesmo estilo da
proteção que já existe para arquivo ausente.

---

## 5. Observação (não é bug): normalização por ranking em `silence_penalty`

`corteia/core/analyze.py:299` (`_normalise`) transforma a carga bruta de silêncio em
posição relativa entre os candidatos. Com uma única faixa muda, por exemplo, o pior
candidato fica em `0.0` e não em `1.0`: **a punição é relativa, nunca absoluta** — um
vídeo inteiro com silêncios longa pontua como se não tivesse nenhum, porque todos os
candidatos empatam. Os testes `tests/test_analyze.py::test_silencio_pontua_pior_que_ausencia_de_silencio`
documentam esse comportamento (assert no `c.raw["silence_penalty"]` bruto + empate em
`0.5` no sinal normalizado). Vale considerar um `raw` → `signal` misto (p.ex.
`0.5 * normalizado + 0.5 * absoluto`) se aparecerem queixas de "cortes ruins aprovados".

---

## 6. Menor: override ASS com a cor duplicada

`corteia/core/subtitles.py` (realce do preset viral) emite
`{\c&H0000E4FF&\c&H0000E4FF&}PALAVRA{\r}` — a mesma tag `\c` duas vezes. O libass ignora a
redundância, mas o texto fica maior que o necessário e confunde ao depurar legenda. Basta
deduplicar as tags antes de fechar o bloco `{...}`.
