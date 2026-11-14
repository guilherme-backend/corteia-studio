# CorteIA Studio

**Você grava uma hora. Ninguém assiste uma hora.**

O CorteIA Studio pega um arquivo de vídeo longo (live, podcast, aula,
entrevista, gameplay) e devolve cortes verticais 9:16 prontos para TikTok,
Reels e Shorts — com legenda queimada, recorte que acompanha quem está falando
e silêncios removidos.

É um app desktop em português do Brasil, com interface PySide6 e FFmpeg real.
Não envia seu vídeo para servidor nenhum: transcrição, detecção de rosto,
pontuação e renderização acontecem na sua máquina. A única saída opcional para
a internet é o download de um modelo Whisper (uma vez) e a avaliação por IA,
que vem desligada.

---

## Sumário

- [O que o programa faz](#o-que-o-programa-faz)
- [Por que ele existe](#por-que-ele-existe)
- [Requisitos reais](#requisitos-reais)
- [Instalação no Windows](#instalação-no-windows)
- [Instalação no Linux e no macOS](#instalação-no-linux-e-no-macos)
- [Primeira execução, passo a passo](#primeira-execução-passo-a-passo)
- [Como escolher o modelo Whisper](#como-escolher-o-modelo-whisper)
- [GPU ou CPU: o que muda](#gpu-ou-cpu-o-que-muda)
- [Atalhos de teclado](#atalhos-de-teclado)
- [Onde os arquivos são gravados](#onde-os-arquivos-são-gravados)
- [Linha de comando, sem interface](#linha-de-comando-sem-interface)
- [Solução de problemas](#solução-de-problemas)
- [Limitações honestas](#limitações-honestas)
- [O que é medido de verdade](#o-que-é-medido-de-verdade)
- [Para desenvolvedores](#para-desenvolvedores)
- [Licença](#licença)

---

## O que o programa faz

Você escolhe um arquivo de vídeo longo. A partir daí, o programa:

1. **Lê o arquivo** com `ffprobe` (duração, resolução, FPS, rotação aplicada,
   se existe trilha de áudio) e gera a miniatura que aparece em cada cartão de
   corte.
2. **Transcreve a fala** com Whisper pelo `faster-whisper`, com marca de tempo
   **por palavra**. É isso que permite legenda sincronizada e corte que não
   corta a palavra no meio.
3. **Caça os silêncios** com o filtro `silencedetect` do FFmpeg.
4. **Detecta planos** (mudanças de câmera) com PySceneDetect, para o corte não
   começar nem terminar em cima de um corte de edição.
5. **Encontra os melhores momentos**: agrupa a transcrição em blocos, gera
   janelas candidatas autossuficientes e pontua cada uma por gancho, palavras
   de retenção, troca de falante, número de planos e fala por segundo, com
   normalização consciente de empates e um portão absoluto de engajamento — um
   trecho sem conteúdo não vence só porque o áudio agitou. Se houver uma API
   de linguagem configurada, ela dá notas complementares; sem API, o pontuador
   local assume sozinho. **Nenhuma etapa obrigatória depende de internet.**
6. **Separa falantes** por heurística acústica (altura da voz e espaçamento
   entre segmentos), não por modelo de diarização — a interface avisa.
7. **Reenquadra em 9:16** seguindo o rosto de quem fala, com suavização e
   limite de velocidade para o recorte não “pular”. Sem rosto, usa saliência
   visual ou recorte central estático.
8. **Remove os silêncios** só dentro do trecho escolhido e recalcula o
   cronograma: a legenda é remontada no tempo do vídeo final, não do original.
9. **Queima a legenda** em ASS (cinco estilos prontos, realce palavra a
   palavra) e, se você quiser, marca d'água, título e trilha musical com
   atenuamento automático quando a fala entra.
10. **Exporta MP4** (H.264/H.265 + AAC, `+faststart`) e **confere o resultado
    com ffprobe** antes de dar o visto verde. Se a duração final divergir do
    esperado além de uma tolerância, a exportação é declarada falha — o app
    nunca reporta sucesso por confiança.

Uma análise gera vários cortes de uma vez. Você aprova os que prestam, rejeita
o resto, ajusta o que quiser no Editor e exporta tudo em lote, com até quatro
renderizações simultâneas.

## Por que ele existe

Cortar manualmente é lento, e a parte lenta não é a parte inteligente. Num
episódio de 60 minutos existem talvez dez trechos que viram um vídeo vertical
de 30 segundos. Achar esses dez exige rebobinar a linha do tempo inteira, um
por um. Depois vem o trabalho mecânico: requadrar para 9:16, digitar a legenda,
sincronizar palavra por palavra, tirar as pausas, renomear, exportar, repetir.

Isso é uma tarde de trabalho por episódio. E o resultado é pior do que parece,
porque a legenda digitada à mão atrasa, o recorte central ignora quem está
falando e os momentos escolhidos são os que você achou antes de sentir sono.

O CorteIA Studio automatiza exatamente a parte mecânica e mantém a decisão
final com você: a máquina sugere, pontua e justifica (“por que este corte”),
mas nada é exportado sem um corte aprovado por você.

## Requisitos reais

| item | mínimo | por quê |
|---|---|---|
| Windows 10/11 (64 bits), Linux ou macOS | — | o app é desktop; não há versão web |
| **Python 3.11 ou mais novo** | 3.12 é a versão usada no desenvolvimento | `run.py` recusa 3.10 com mensagem em português e código de saída 4 |
| **FFmpeg com `libx264` e `libass`** | FFmpeg 6.x | sem `libx264` a exportação falha; sem `libass`/filtro `subtitles` a legenda **não é queimada** |
| `libx265` (opcional) | — | só necessário para exportar em H.265; sem ele, use H.264 |
| **8 GB de RAM** | 16 GB confortável | com 8 GB use Whisper `tiny` ou `base`; `medium`/`large-v3` estouram |
| Espaço em disco | ~1 GB de dependências + modelos | o `large-v3` são ~3,1 GB baixados e ~6,2 GB de pico de RAM |
| CPU 4 núcleos | 8+ núcleos confortável | a codificação é por CPU neste ambiente de referência |
| Fonte do sistema para a legenda | Arial/Segoe UI/DejaVu/Noto | o app avisa quando substitui uma fonte ausente |
| Internet | só para baixar o modelo Whisper escolhido | o resto roda offline; download por URL (yt-dlp) é opcional e exige consentimento |

O FFmpeg **não** é dependência de `pip`: é um binário nativo à parte, no `PATH`
ou ao lado do executável. Os instaladores conferem isso para você.

---

## Instalação no Windows

Você tem duas rotas. Escolha uma.

### Rota A — instalador (duplo clique)

É o caminho de quem só quer usar o programa, sem tocar em terminal.

1. Baixe `CorteIA-Setup-1.0.0.exe`.
2. Dê **duplo clique**. O Windows vai mostrar o aviso do SmartScreen
   (“O Windows protegeu seu computador”) porque o instalador **não tem
   assinatura de código**. Clique em **Mais informações → Executar mesmo
   assim**. Não é vírus, é falta de certificado: veja
   [docs/WINDOWS.md](docs/WINDOWS.md).
3. Siga o assistente. Ele cria atalhos no menu Iniciar e na área de trabalho
   (opcional) e registra a desinstalação no Painel de Controle.
4. Se o Windows pedir, instale o **Visual C++ 2015-2022 x64 Redistributable**
   (o instalador avisa e oferece o link). As DLLs do Qt e do CTranslate2
   dependem dele numa máquina limpa.
5. Instale o FFmpeg, se ainda não tiver (o assistente avisa). O jeito mais
   simples, num `cmd`:

   ```
   winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
   ```

   **Feche e reabra** qualquer terminal depois disso: o `PATH` novo só vale
   para sessões novas.

6. Abra “CorteIA Studio” no menu Iniciar.

O instalador é gerado por quem desenvolve, com `build_windows.bat`. Ele não foi
executado no ambiente onde este README foi escrito (não há Windows nem Inno
Setup lá), então o roteiro de verificação numa máquina limpa está em
[docs/WINDOWS.md](docs/WINDOWS.md).

### Rota B — a partir do código (usa internet)

Se você baixou/cloneou a pasta do projeto em vez do instalador:

1. Instale o Python 3.11+ de <https://www.python.org/downloads/windows/>
   marcando **“Add python.exe to PATH”**.
2. Instale o FFmpeg (o comando `winget` acima).
3. Dê **duplo clique em `install.bat`**. Ele cria `.venv`, instala as
   dependências de `requirements.txt`, confere FFmpeg/`libx264`/`libx265`/
   `libass` e reporta a máquina. Se você rodar `install.bat --with-ffmpeg`,
   ele chama o `winget` sozinho quando o FFmpeg estiver ausente.
4. Dê **duplo clique em `run.bat`** para abrir o programa. `run.bat --smoke`
   testa o pipeline inteiro sem abrir janela.

Para saber se está tudo certo antes de instalar qualquer coisa:
`install.bat --check-only`.

## Instalação no Linux e no macOS

1. Precisa de Python 3.11+ e FFmpeg. No Debian/Ubuntu:

   ```bash
   sudo apt update && sudo apt install -y python3 python3-venv python3-pip ffmpeg
   ```

   No macOS, com Homebrew: `brew install python ffmpeg`.

2. Entre na pasta do projeto e rode o instalador:

   ```bash
   cd corteia-studio
   bash install.sh
   ```

   Quer que ele instale o FFmpeg também (apt/dnf/pacman/brew)? `bash
   install.sh --with-ffmpeg`. Só auditar sem instalar nada? `bash install.sh
   --check-only`. Para desenvolver (pytest/ruff), `bash install.sh --dev`.

3. Abra o programa:

   ```bash
   ./run.sh
   ```

   ou, com o ambiente ativo:

   ```bash
   . .venv/bin/activate
   python run.py
   ```

O instalador do Linux/macOS não gera pacote (.deb/.dmg): ele cria o ambiente
`.venv` dentro da pasta do projeto. Para um executável de Linux/macOS, veja
`build_linux.sh` (gera `dist/CorteIA Studio/`).

**Wayland e X11.** No Linux, o player de vídeo usa `QMediaPlayer`. Em Wayland
com drivers recentes tudo funciona; se o vídeo não aparecer na interface,
force a plataforma com `QT_QPA_PLATFORM=xcb ./run.sh`. Este é um caso comum em
distros com Mesa antigo e vale testar antes de reportar bug.

## Primeira execução, passo a passo

A primeira vez que o programa abre, ele cria as pastas de dados e escreve o
`config.json` com os padrões. Não há cadastro nem login.

### 1 · Importar

Barra lateral → **Importar** (atalho `Ctrl+2`). Você tem três formas de dizer
qual é o vídeo:

- **Arrastar** o arquivo para a área tracejada (funciona arrastar para
  qualquer lugar da janela, que já cai nesta página);
- **Escolher arquivo**, o botão de diálogo;
- **Usar link**, que baixa com yt-dlp — e **recusa** enquanto você não ligar
  o consentimento em Configurações → Download, porque baixar vídeo de site é
  algo que o programa não faz por conta própria.

Formatos aceitos: `.mp4 .m4v .mov .mkv .webm .avi .mpg .mpeg .mts .m2ts .ts
.wmv .flv .3gp .ogv .vob .divx` (`.3g2` também).

Logo abaixo do seletor de arquivo aparecem a duração, a resolução, o FPS e a
presença de áudio, lidos com `ffprobe` de verdade — e a escolha do **modelo
Whisper**. Se o modelo ainda não estiver baixado, o botão **Baixar modelo**
puxa da Hugging Face (só isso precisa de internet). O programa também mostra
uma estimativa de tempo de transcrição: ela vem do histórico de medições
*desta máquina*, não de um número inventado; na primeira vez, enquanto não há
histórico, a estimativa é marcada como conservadora.

Escolha também a duração-alvo dos cortes (15/30/45/60/90/120 s ou um intervalo
personalizado), quantos cortes sugerir, a nota mínima e a proporção (9:16 é o
padrão).

### 2 · Analisar

Clique **Analisar e gerar cortes** (`Ctrl+G`). A barra superior mostra seis
estágios e o progresso global:

| estágio | o que acontece |
|---|---|
| Lendo o arquivo | `ffprobe` |
| Miniatura | um quadro aos 10% do vídeo, em cache |
| Transcrevendo fala | Whisper, com marca de tempo por palavra |
| Separando falantes | heurística acústica (só se “seguir falante” estiver ligado) |
| Caçando silêncios | `silencedetect` |
| Detectando planos | PySceneDetect |
| Avaliando com IA | nota do modelo de linguagem, **se** houver API; senão “modelo local (sem API)” |
| Montando os cortes | candidatos → pontuação → seleção → títulos |

Cada estágio grava um *checkpoint* no projeto. Você pode fechar o programa no
meio de um vídeo de 40 minutos e o que já foi feito não se perde. `Esc`
cancela — e cancela de verdade, até no meio do FFmpeg.

Reanalisar o mesmo arquivo não retranscreve: o texto fica no projeto e, se ele
ainda não estiver lá, há um cache de transcrição em disco por
(arquivo, modelo, idioma).

### 3 · Revisar os Cortes

Barra lateral → **Cortes** (`Ctrl+3`). Cada cartão mostra miniatura real,
duração, início→fim no original, título e descrição, a barra de pontuação
(passando o mouse aparecem os sinais que compuseram aquela nota), quantos
falantes e quantos planos, e o motivo escolhido em texto.

Aqui você decide: **aprovar**, **rejeitar**, marcar a seleção, **abrir no
editor** ou **exportar a seleção**. Ao reanalisar, cortes já aprovados ou já
exportados **sobrevivem** — sua decisão vale mais que a da máquina.

### 4 · Editor

Barra lateral → **Editor** (`Ctrl+4`). Player com loop de faixa, linha do tempo
arrastável com alças de início/fim (os campos aceitam `mm:ss`) e cinco abas:

- **Legendas** — lista editável de segmentos (grava em `clip.edit
  .manual_text_edits`) e todos os campos de estilo com prévia ao vivo. Os
  estilos prontos: *Viral*, *Profissional*, *Podcast* (karaokê palavra a
  palavra), *Dinâmico* e *Minimalista*.
- **Enquadramento** — proporção, zoom, deslocamento X/Y, recorte manual e
  prévia do recorte.
- **Áudio** — volume, música de fundo, atenuamento da música quando a fala entra.
- **Identidade** — marca d'água (imagem ou texto, posição, opacidade,
  tamanho) e título com tarja.
- **Silêncio** — ligar/desligar a remoção de pausas neste corte específico.

Tudo que você mexe aqui escreve em `clip.edit` e salva o projeto. Nada é
“só visual”: o que a prévia mostra é o que o FFmpeg vai fazer.

### 5 · Exportar

Barra lateral → **Exportar** (`Ctrl+5`, atalho `Ctrl+E`). A página mostra uma
pré-análise honesta: quantos cortes, quantos MB e quantos minutos estimados. A
velocidade usada nessa conta vem do **seu histórico de exportações** neste
computador; sem histórico, o app diz que a estimativa ainda não é medida.

Escolha a pasta de destino, o codificador (H.264 ou H.265), a resolução
(720p/1080p/4K), o FPS, o CRF (quanto menor, melhor) e o preset de
velocidade. Clique em **Exportar**. A fila mostra o progresso por corte.

Ao terminar, o arquivo está onde você escolheu, com o nome do padrão
`{index:02d}-{title}-corteia` por exemplo `03-O segredo dos 3 segundos-corteia.mp4`.
O botão **Abrir pasta** leva direto. O MP4 já foi re-medido com `ffprobe`:
o visto verde significa que o arquivo existe, tem a resolução pedida e a
duração que o cronograma prometia.

---

## Como escolher o modelo Whisper

O nome dos modelos abaixo é exatamente o que aparece no seletor do programa
(campo `whisper_model` em `AnalysisOptions`, chave `subs.model` na
configuração). “Tamanho” é o peso baixado; “pico de RAM” é o que o próprio app
mostra na tela como aviso; “tempo p/ 10 min de áudio” é a saída real de
`transcribe.estimate_duration_hours()` **neste** computador (CPU de 6 núcleos,
sem GPU) — repare que dois números vêm de medição acumulada pela máquina e os
outros de estimativa conservadora, porque ainda não foram rodados aqui.

| modelo | tamanho | pico de RAM | tempo p/ 10 min de áudio | qualidade esperada |
|---|---|---|---|---|
| `tiny` | 78 MB | ~380 MB | **≈ 37 s** (medido, 0,06× do tempo real) | rascunho e teste. Erra palavras em áudio ruim, nomes próprios e sotaque forte; em estúdio limpo é utilizável |
| `base` (padrão) | 148 MB | ~620 MB | **≈ 3 min 17 s** nesta tabela, 0,08–0,33× em medições reais | melhor custo/benefício em CPU. É o ponto de partida certo para quase todo mundo |
| `small` | 486 MB | ~1,5 GB | ≈ 19 min (estimativa conservadora; rode uma transcrição para calibrar) | precisão bem maior; ~3× mais lento que o `base` |
| `medium` | 1.531 MB (1,5 GB) | ~3,6 GB | ≈ 44 min (estimativa conservadora) | exige 16 GB de RAM ou placa de vídeo |
| `large-v3` | 3.091 MB (3,1 GB) | ~6,2 GB | ≈ 1 h 36 min (estimativa conservadora) | máxima precisão, principalmente com GPU. Em CPU é paciência pura |
| `distil-large-v3` | 1.517 MB (1,5 GB) | ~3,2 GB | ≈ 40 min (estimativa conservadora) | quase o `large-v3` por bem menos CPU |
| `auto` | — | — | — | o app escolhe pelo total de RAM e pela duração do vídeo (aqui, com 23,8 GB, `auto` para um vídeo de 10 min decidiu `small`) |

Regras práticas:

- **Uso geral, CPU, 8–16 GB de RAM:** `base`. Se a transcrição sair cheia de
  erro, suba para `small` só no vídeo que importa.
- **Vou revisar a legenda manualmente de qualquer forma:** `tiny` para achar
  os momentos rápido, e aí o Editor faz o resto (editar a legenda não muda o
  corte nem a duração).
- **Entrevista ruidosa, sotaque, jargão, nome próprio importante:** `small`
  para cima. O `tiny` troca “retenção” por “retenção” e isso vai para a
  legenda queimada.
- **16 GB de RAM ou mais / GPU NVIDIA com 8 GB de VRAM:** `medium`/`large-v3`
  deixam de ser absurdos.
- O modelo é escolhido **por análise**. Trocar de modelo obriga a retranscrever
  (um cache novo por modelo; o velho continua lá).
- Vocabulário do seu nicho pode ser injetado no Whisper pelo campo de prompt
  inicial (`condition_on`/`initial_prompt` em `transcribe()`), o que melhora
  termos técnicos sem trocar de modelo.

Por que a tabela mostra tempos tão diferentes para o `base`? Porque tempo de
transcrição depende do número de threads, do áudio e se o modelo já estava
carregado. As duas medições desta entrega deram 0,082× com todos os 6 núcleos e
0,3285× quando o app limitou a 5 threads num clipe de 20 s; o arquivo
`~/.cache/corteia-studio/perf.json` guarda o histórico e é com ele que o app
estima o tempo restante. Se a estimativa parecer errada, rode uma transcrição
real: a estimativa passa a ser medida.

## GPU ou CPU: o que muda

| etapa | com GPU NVIDIA | com CPU |
|---|---|---|
| Transcrição (faster-whisper / CTranslate2) | muito mais rápida; `device="cuda"`, `compute_type="float16"` | `int8`, que é cerca de 2× mais rápido e usa metade da memória do `float32` |
| Detecção de rostos (YuNet) | CPU (OpenCV DNN neste build) | CPU — 11,1 ms por quadro em 1080p, medido |
| Detecção de planos | CPU | CPU |
| Codificação do MP4 | depende de você selecionar um encoder de GPU em Configurações → Exportar (`h264_nvenc`, `hevc_nvenc`, `h264_amf`, `h264_qsv`, `hevc_qsv`, `h264_videotoolbox`), se o FFmpeg os tiver | `libx264`/`libx265`, qualidade controlada por CRF |

O ponto que mais gente entende errado, e que o programa trata explicitamente:

> **NVENC no FFmpeg não é prova de CUDA ativo.** Muitos builds de FFmpeg trazem
> o encoder `h264_nvenc` compilado, mesmo em máquina sem placa NVIDIA. Se o app
> visse esse nome e pedisse `device="cuda"` ao CTranslate2, a transcrição
> quebraria com “CUDA driver version is insufficient”. Por isso
> `hardware.cuda_available` só é verdadeiro com `nvidia-smi` respondendo **e**
> runtime CUDA funcional.

É por isso que o `--check` deste ambiente lista NVENC e ainda assim avisa que a
codificação será por CPU. Transcrição em GPU não muda nada na qualidade do
texto, só na velocidade (e permite `medium`/`large-v3` sem sofrimento).

Um caso especial: se você tem uma GPU Intel ou AMD, o que ajuda é o encoder de
vídeo (`h264_qsv` / `h264_amf`), que acelera a exportação; a transcrição
continua por CPU, porque CUDA é NVIDIA.

O que `python run.py --check` diz sobre isto, na máquina onde este README foi
escrito (saída transcrita literal, Python 3.12.3 no venv do projeto):

```text
App                    CorteIA Studio 1.0.0
Python                 3.12.3 em /home/ubuntu/corteia-studio/.venv/bin/python
CPU / RAM              6 núcleos · 23.8 GB (21.2 GB livres)
GPU                    não detectada
FFmpeg                 /usr/bin/ffmpeg  (6.1.1-3ubuntu5)
  codec libx264       ok
  codec libx265       ok
  codec aac           ok
  legendas (libass)   ok
  aceleração GPU      h264_nvenc, hevc_nvenc, h264_qsv, hevc_qsv
Whisper                tiny instalado · base instalado · small não baixado
Fontes instaladas      1629
Config                 /home/ubuntu/.config/corteia-studio/config.json
Cache                  /home/ubuntu/.cache/corteia-studio
Temp                   /tmp/corteia-studio (387.3 KB)
Projetos               /home/ubuntu/.config/corteia-studio/projects
  aviso: Este FFmpeg tem NVENC, mas não há runtime CUDA ativo: a codificação será por CPU. NVENC exige driver NVIDIA instalado.
Tudo pronto para uso.
```

Código de saída 0. Interpretação linha a linha: os três codecs necessários
estão presentes (`libx264` para H.264, `libx265` para H.265, `aac` para o
áudio — este último costuma sumir em builds “essentials” mal montados), o
filtro `subtitles` existe, ou seja, **a legenda será queimada**, e a linha
“aceleração GPU” lista os encoders de GPU que o FFmpeg compilou — sem que isso
signifique que há GPU na máquina, como o aviso abaixo deixa claro. O app fecha
o `--check` com `1` e uma lista `PROBLEMAS:` quando falta FFmpeg, falta
`libx264`, falta o filtro de legendas ou nenhuma fonte de título foi localizada.

## Atalhos de teclado

Na janela inteira (`corteia/ui/windows/main_window.py`):

| tecla | o que faz |
|---|---|
| `Ctrl+1` … `Ctrl+6` | Início, Importar, Cortes, Editor, Exportar, Configurações |
| `Ctrl+O` | abrir um projeto salvo |
| `Ctrl+S` | salvar o projeto |
| `Ctrl+I` | ir para Importar e focar o campo do arquivo |
| `Ctrl+G` | **analisar e gerar cortes** (usa o trabalho real da página Importar) |
| `Ctrl+E` | ir para Exportar |
| `Ctrl+,` | Configurações |
| `Esc` | cancelar a operação em andamento |
| arrastar um arquivo para a janela | importar (funciona em qualquer página) |

Com a linha do tempo do Editor focada (`corteia/ui/widgets/timeline.py`):

| tecla | o que faz |
|---|---|
| `←` / `→` | mover um quadro (25 fps nominal) |
| `Shift+←` / `Shift+→` | mover 1 segundo |
| `I` | marcar o **início** do corte na posição atual |
| `O` | marcar o **fim** do corte |
| `L` | ligar/desligar o loop da faixa selecionada |
| `F` | ajustar a linha do tempo ao corte |
| `+` / `-` | aproximar / afastar o zoom |
| rolagem do mouse | deslocar a linha do tempo |
| `Ctrl` + rolagem | zoom centrado no cursor |

Em qualquer botão de alternância focado, `Espaço` ou `Enter` liga/desliga.

## Onde os arquivos são gravados

Caminhos reais deste computador (Linux), reportados pelo próprio `--check`. No
Windows e no macOS o programa usa o padrão de cada sistema, com o mesmo
esquema de subpastas (`corteia/paths.py` é a fonte da verdade).

| o que | Linux (real, nesta máquina) | Windows | macOS |
|---|---|---|---|
| Configuração | `~/.config/corteia-studio/config.json` | `%APPDATA%\CorteIA Studio\config.json` | `~/Library/Application Support/CorteIA Studio/config.json` |
| Projetos | `~/.config/corteia-studio/projects/` | `%APPDATA%\CorteIA Studio\projects\` | `~/Library/Application Support/CorteIA Studio/projects/` |
| Modelo Whisper baixado | `~/.config/corteia-studio/models/` | `%APPDATA%\CorteIA Studio\models\` | `~/Library/Application Support/CorteIA Studio/models/` |
| Índice de recents / estatísticas | `~/.config/corteia-studio/recents.json`, `stats.json` | `%APPDATA%\CorteIA Studio\` | idem, em Application Support |
| Log | `~/.config/corteia-studio/logs/corteia.log` | `%APPDATA%\CorteIA Studio\logs\corteia.log` | `~/Library/Application Support/CorteIA Studio/logs/corteia.log` |
| Cache (miniaturas, transcrições, `perf.json`, QSS compilado) | `~/.cache/corteia-studio/` | `%LOCALAPPDATA%\CorteIA Studio\Cache\` | `~/Library/Caches/CorteIA Studio/` |
| Temporários de render | `/tmp/corteia-studio/` | `%LOCALAPPDATA%\CorteIA Studio\Temp\` | `$TMPDIR` / `/tmp/corteia-studio/` |
| **Vídeos exportados** | `~/Videos/CorteIA/` | `%USERPROFILE%\Videos\CorteIA\` | `~/Videos/CorteIA/` (a pasta é criada se não existir) |

O destino de exportação é o mesmo esquema em todos os sistemas porque
`paths.exports_dir_default()` usa `~/Videos/CorteIA` sem ramificação de
plataforma — no macOS não é `~/Movies`. Você muda isso em Configurações →
Exportar (chave `export.dir`).

Detalhes que valem saber:

- O cache de transcrição vive em `~/.cache/corteia-studio/transcripts/`, com
  um nome derivado de (arquivo, modelo, idioma). Por isso a segunda análise do
  mesmo vídeo é ~59× mais rápida: ela pula o Whisper.
- As miniaturas ficam em `~/.cache/corteia-studio/thumbs/` (28 delas aqui).
- O `.ass` da legenda é gravado **ao lado do MP4 final** no momento da
  exportação; se você quiser legenda separada, o Editor também exporta SRT/VTT
  (`subtitles.to_srt` / `to_vtt`).
- O temporário de render passa por uma pasta controlada, `/tmp/corteia-studio`,
  nunca pelo lado do seu arquivo; há limite configurável (`proc.temp_limit_gb`)
  e limpeza (`paths.clean_temp`).
- Nada sai da sua máquina, exceto: baixar modelo (Hugging Face) e, se você
  ligar, chamadas de API de linguagem e download por URL. Telemetria está
  desligada por padrão (`misc.telemetry: false`) e não há verificação automática
  de atualização (`misc.check_updates: false`).

## Linha de comando, sem interface

Serve para diagnosticar, automatizar e testar. Tudo abaixo funciona de dentro
do venv (`. .venv/bin/activate`) ou pelos lançadores (`./run.sh`, `run.bat`).

| comando | o que faz |
|---|---|
| `python run.py` | abre a interface |
| `python run.py --check` | diagnóstico de FFmpeg, codecs, libass, GPU, fontes, modelos e caminhos (acima); código 0 quando está tudo pronto |
| `python run.py --smoke` | gera um vídeo real de 22 s com fala PT-BR (espeak-ng) e silêncio entre 9 e 11 s, roda **todo** o pipeline, exporta e re-mede os MP4 com ffprobe; sai 0 ou 1 |
| `python run.py --analyze VIDEO` | analisa sem GUI e grava `VIDEO.cortes.json` ao lado do arquivo, com `start`, `end`, `duration`, `title`, `description`, `score`, `reason`, `keywords` |
| `python run.py --analyze VIDEO --json SAIDA.json` | mesmo, escolhendo o destino do JSON |
| `python run.py --export VIDEO INÍCIO SEGUNDOS` | recorta e renderiza um trecho sem interface; grava em `~/Videos/CorteIA/` (ex.: `--export aula.mp4 12 30`) |
| `python run.py --version` | `CorteIA Studio 1.0.0` |
| `python run.py -v` / `--verbose` | log `DEBUG` no console (útil com `--check`) |
| `python run.py --min-size 800` | tamanho mínimo do recorte, repassado à interface |
| `corteia --help` | o mesmo app, pelo comando instalado (`pip install -e .`) |

Exemplo real de uso em lote, sem abrir janela:

```bash
python run.py --analyze podcast-ep12.mp4          # vejo as sugestões no JSON
# ajusto o que quiser no arquivo .cortes.json
python run.py --export podcast-ep12.mp4 284 32    # exporto 32 s a partir de 4m44s
```

---

## O que o `--check` diz sobre a sua máquina

Abra um terminal na pasta do programa e rode:

```
CorteIA Studio.exe --check
```

Além de FFmpeg, codecs, libass e fontes, a saída agora fecha com o **orçamento de
memória do Whisper**: para cada modelo (`tiny` … `large-v3`, `distil-large-v3`) ele
imprime `ok` ou `NÃO CABE` com o número que sustentou a decisão — a fração usada da
VRAM (85 %) ou da RAM livre (70 %) — e termina com a linha `recomendado`, o maior
modelo que roda naquela máquina. Em uma RTX 3050 de 6 GB, por exemplo, `small` é o
maior que cabe em `cuda/float16`; `medium` pediria ~6,7 GB e aparece como `NÃO CABE`.

Isso não é só informativo: o carregador do Whisper consulta a mesma conta **antes** de
tentar abrir o modelo. Se o ajuste pedir mais do que a placa ou a RAM suportam, ele
troca para `cpu/int8` quando o modelo cabe ali, ou recusa com uma mensagem que diz qual
modelo usar — em vez de estourar memória ou fechar o programa. O fallback continua
valendo para CUDA ausente ou driver incompatível.
