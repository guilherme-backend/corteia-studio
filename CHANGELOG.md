# Changelog

Todas as mudanças notáveis neste projeto estão documentadas neste arquivo.

O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-br/1.1.0/),
e este projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

## [1.0.0] - 2026-11-14

Primeira versão pública do CorteIA Studio: do arquivo longo ao MP4 vertical
com legenda queimada, sem editar nada à mão.

### Added

- **Pipeline de análise com um clique** (`corteia/core/pipeline.py`): lê o
  arquivo com `ffprobe`, gera miniatura, transcreve com Whisper, separa
  falantes, mapeia silêncios e planos, avalia com IA (opcional) e devolve os
  cortes pontuados. Seis estágios visíveis na interface, com checkpoint depois
  de cada um — interromper no meio de um vídeo de 40 minutos não joga o
  trabalho fora.
- **Transcrição com faster-whisper** (`corteia/core/transcribe.py`): modelos
  `tiny` → `large-v3` e `distil-large-v3`, marcas de tempo por palavra,
  idioma PT-BR por padrão, cache de transcrição em disco (a segunda análise do
  mesmo arquivo não roda o Whisper de novo) e medição real de velocidade por
  máquina guardada em `perf.json`, usada para estimar o tempo restante.
- **Detecção dos melhores momentos** (`corteia/core/analyze.py`): agrupa a
  transcrição em blocos autocontidos, enumera janelas candidatas, pontua
  gancho, densidade de palavras-chave, emoção, troca de falante, cortes de
  cena e ritmo de fala, remove sobreposições (IoU) e estende o título/descrição
  gerados pelo dicionário PT-BR de `corteia/core/lexicon.py`.
- **Corte vertical que segue o falante** (`corteia/core/faces.py`,
  `reframe.py`): detector YuNet (ONNX) com fallback para Haar e, na falta dos
  dois, recorte por saliência ou central estático; suavização por média móvel
  exponencial com limite de velocidade para o enquadramento não “pular”.
- **Legendas queimadas** (`corteia/core/subtitles.py`, `render.py`): ASS gerado
  palavra a palavra com cinco presets de estilo (viral, profissional, podcast,
  dinâmico, minimalista), realce por importância, e sincronia recalculada pelo
  `TimelineMap` quando há silêncio removido.
- **Remoção de silêncios** (`corteia/core/silence.py`, `timeline.py`):
  `silencedetect` do FFmpeg, junção de pausas próximas e recorte só nos pontos
  que não cortam palavra.
- **Fila de exportação** (`corteia/core/exporter.py`): exporta os cortes
  aprovados com até 4 tarefas simultâneas, cancela entre cortes e dentro do
  FFmpeg, e confere o MP4 gerado com `ffprobe` antes de mostrar o visto verde.
- **Interface PySide6 em português do Brasil** (`corteia/ui/`): seis páginas
  (Início, Importar, Cortes, Editor, Exportar, Configurações), tema escuro em
  QSS, atalhos de teclado, arrastar-e-soltar global, leitor de vídeo com loop
  de faixa, linha do tempo arrastável e prévia de legenda desenhada ao vivo.
- **Modos sem interface** (`run.py` / `corteia/main.py`): `--check`, `--smoke`,
  `--analyze VIDEO` (grava `VIDEO.cortes.json`), `--export VIDEO INÍCIO
  SEGUNDOS` e `--version`, para diagnóstico, CI e automação em lote.
- **Download por URL** (`corteia/core/downloader.py`): yt-dlp só roda depois
  de o usuário ligar o consentimento em Configurações → Download.
- **Instaladores** (`install.sh`, `install.bat`) e **lançadores** (`run.sh`,
  `run.bat`) que verificam Python 3.11+, FFmpeg e codecs antes de instalar.
- **Empacotamento** (`corteia.spec`, `build_linux.sh`, `build_windows.bat`,
  `installer/corteia.iss`): PyInstaller one-dir para Linux/macOS e instalador
  Inno Setup 6 para Windows.
- **Documentação em português**: `README.md`, `docs/ARQUITETURA.md`,
  `docs/API.md`, `docs/TESTES.md`, `docs/WINDOWS.md`, `LICENSE.txt` (MIT, o
  `pyproject.toml` já apontava para esse arquivo), `.gitignore`,
  `.editorconfig` e este `CHANGELOG.md`.

### Fixed

- Leitura dos encoders do FFmpeg: o parser usava `split()[0].startswith(" A")`,
  mas `split()` consome o espaço inicial — por isso `aac` aparecia como
  ausente no `--check` mesmo presente.
- CUDA falso-positivo: ter `h264_nvenc` no FFmpeg não significa runtime CUDA
  ativo; entregar `device="cuda"` ao CTranslate2 nesse caso causava falha.
- Progresso de 58% travado no indicador: um estágio já concluído podia ser
  rebaixado a “em andamento” pelo último evento de progresso.
- Legendas invisíveis: a linha `Style:` do ASS saía com cores no formato de
  *override tag* e 25 campos em vez de 23, e o libass não desenhava nada.
- Karaokê `\kf` tingia todo o texto de amarelo no estilo viral; agora o
  efeito é restrito aos estilos podcast e dinâmico.
- Tamanho de saída: `render.output_size()` devolvia 608×1080 para 9:16 em
  1080p; “1080p” nomeia o lado curto, então o correto é 1080×1920.
- Fronteiras de corte reintroduziam o silêncio removido quando os blocos
  eram forçados a ser contíguos.
- Marca d'água fora da tela: `drawtext` recebia expressões de `overlay`
  (`w` = largura do vídeo) em vez de `text_w`/`text_h`.
- Salvar projeto quebrava com `TypeError` (`__dict__` em dataclass
  `slots=True`); a serialização usa `asdict`.
- `Config.bool_()`/`int_()`/`float_()`/`str_()` passaram a aceitar valor
  padrão, o que destravava a exportação quando a chave não existia.
- Fontes: o sorteio de faces do `fc-list` escolhia por vezes um itálico ou um
  extra-light; agora as faces são ranqueadas (nunca itálico, negrito quando
  pedido).

### Known issues

Agrupadas aqui para não surpreender ninguém depois da instalação; a lista
completa e o que fazer a respeito estão em `README.md` → *Limitações*.

- A separação de falantes **não** usa modelo de diarização: é heurística
  acústica (altura + espaçamento entre segmentos), e a interface avisa.
- Sem codificação por GPU neste ambiente de referência: o `--check` lista
  NVENC/Quick Sync porque o FFmpeg os compilou, mas não há runtime CUDA ativo,
  então a codificação é por CPU.
- O `Whisper tiny` troca palavras em áudio ruim; `base` é o padrão por causa
  disso.
- O download por URL (yt-dlp) foi validado com payloads sintéticos, não contra
  sites reais.
- O instalador do Windows não foi executado: não há Windows nem Inno Setup no
  ambiente de referência. Não há assinatura de código, então o SmartScreen
  exibirá o aviso de editor desconhecido na primeira execução.

### Notes

- Números medidos nesta máquina (6 núcleos, 23,8 GB de RAM, FFmpeg 6.1.1, sem
  GPU) estão no bloco “O que é medido de verdade” do `README.md`.
- Requisito de Python 3.11+; desenvolvido e testado em 3.12.3.
