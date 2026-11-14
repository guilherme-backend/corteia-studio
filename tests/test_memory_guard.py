"""Guarda de memória e fallback de dispositivo do Whisper (sem GPU real).

Estes testes simulam a máquina do usuário-alvo -- Ryzen 5 4600G, RTX 3050 de
6 GB de VRAM, 16 GB de RAM -- e a mesma máquina sem driver CUDA, usando mocks.
Eles não dependem de rede, de modelo baixado nem de placa: o `WhisperModel`
falso registra o que foi pedido e opcionalmente falha como uma placa real
falharia (cuDNN ausente, driver velho).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from corteia.core import transcribe as TR
from corteia.core.errors import TranscriptionError
from corteia.hardware import HardwareReport, probe


class FakeModel:
    """`faster_whisper.WhisperModel` que se recusa a rodar em CUDA sob demanda."""

    def __init__(self, directory, *, device="cpu", compute_type="int8", **kw):
        if device == "cuda":
            raise RuntimeError("CUDA driver error: libcudnn_cnn_infer.so not found")
        self.device, self.compute_type = device, compute_type


def build_fw():
    """Módulo faster_whisper falso cujo construtor vira o FakeModel."""
    return type("FW", (), {"WhisperModel": FakeModel})


@pytest.fixture()
def sem_download(monkeypatch, tmp_path):
    """Nada é baixado e nenhum modelo real é lido do disco."""
    monkeypatch.setattr(TR, "is_downloaded", lambda name: True)
    monkeypatch.setattr(TR, "model_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(TR, "_CACHE", {})


def hardware(monkeypatch, *, vram: float, cuda: bool, ram_total=16.0,
             ram_free=11.8):
    """Instala um probe falso com a placa pedida e a RAM do alvo."""
    base = probe()
    fake = dataclasses.replace(base, gpu_vendor="NVIDIA" if cuda else "AMD",
                               gpu_name="NVIDIA GeForce RTX 3050 6GB" if cuda
                               else "AMD Radeon Graphics",
                               gpu_vram_gb=vram, cuda_available=cuda,
                               nvenc_available=cuda, ram_gb=ram_total,
                               ram_available_gb=ram_free)
    monkeypatch.setattr("corteia.hardware.probe", lambda: fake)
    monkeypatch.setattr(TR, "_ram_total_gb", lambda: ram_total)
    monkeypatch.setattr(TR, "_ram_available_gb", lambda: ram_free)
    return fake


# --------------------------------------------------------------------------- #
# orçamento de memória                                                        #
# --------------------------------------------------------------------------- #
def test_orcamento_de_vram_usa_85_por_cento_da_placa(monkeypatch):
    hardware(monkeypatch, vram=6.0, cuda=True)
    budget, why = TR.memory_budget("cuda", "float16")
    assert budget == pytest.approx(6.0 * 0.85)
    assert "VRAM" in why


def test_float16_custa_quase_o_dobro_de_int8():
    assert TR.footprint_gb("medium", "float16") > 2 * TR.footprint_gb("medium", "int8") * 0.9


def test_medium_nao_cabe_em_6_gb_de_vram_mas_cabe_em_16_gb_de_ram(monkeypatch):
    hardware(monkeypatch, vram=6.0, cuda=True)
    ok_gpu, why_gpu = TR.check_fit("medium", "cuda", "float16")
    assert ok_gpu is False and "medium" in why_gpu
    ok_cpu, _ = TR.check_fit("medium", "cpu", "int8")
    assert ok_cpu is True


def test_maior_modelo_que_cabe_na_rtx_3050_e_small(monkeypatch):
    hardware(monkeypatch, vram=6.0, cuda=True)
    assert TR.largest_fitting("cuda", "float16") == "small"


def test_auto_na_maquina_alvo_escolhe_small(monkeypatch):
    hardware(monkeypatch, vram=6.0, cuda=True)
    assert TR.resolve_model("auto", audio_seconds=600) == "small"


def test_auto_com_uma_hora_de_audio_desce_um_degrau(monkeypatch):
    hardware(monkeypatch, vram=6.0, cuda=True)
    curto = TR.resolve_model("auto", audio_seconds=600)
    longo = TR.resolve_model("auto", audio_seconds=5400)
    assert TR._TIER.index(longo) < TR._TIER.index(curto)


def test_auto_sem_gpu_usa_apenas_a_regra_de_ram(monkeypatch):
    hardware(monkeypatch, vram=6.0, cuda=False)
    assert TR.resolve_model("auto", audio_seconds=600) in ("base", "small", "medium")


# --------------------------------------------------------------------------- #
# comportamento do carregador                                                 #
# --------------------------------------------------------------------------- #
def test_carregador_nao_tenta_o_que_nao_cabe_na_vram(monkeypatch, sem_download):
    """large-v3 em 6 GB de VRAM: cai para CPU/int8 com aviso, sem estourar."""
    hardware(monkeypatch, vram=6.0, cuda=True)
    monkeypatch.setattr(TR, "_faster_whisper", lambda: type(
        "FW", (), {"WhisperModel": lambda directory, **kw: object()}))
    model, dev, ct = TR._get_model("large-v3", "cuda", "float16", 4)
    assert (dev, ct) == ("cpu", "int8")
    assert "VRAM" in TR.memory_note() or "não cabe" in TR.memory_note()


def test_cuda_que_falha_vira_cpu_int8_sem_erros_de_driver(monkeypatch, sem_download):
    """Driver ausente: o fallback já existia; o teste trava o comportamento."""
    hardware(monkeypatch, vram=6.0, cuda=True)
    monkeypatch.setattr(TR, "_faster_whisper", build_fw)
    model, dev, ct = TR._get_model("base", "cuda", "float16", 4)
    assert (dev, ct) == ("cpu", "int8")


def test_modelo_que_nao_cabe_em_lugar_nenhum_da_erro_claro(monkeypatch, sem_download):
    """1 GB de RAM livre: melhor opção é tiny, e o erro diz isso."""
    hardware(monkeypatch, vram=0.0, cuda=False, ram_total=2.0, ram_free=0.6)
    with pytest.raises(TranscriptionError) as exc:
        TR._get_model("large-v3", "cpu", "int8", 2)
    msg = f"{exc.value.message} {exc.value.detail}"
    assert "large-v3" in msg and "tiny" in msg


def test_erro_nao_contem_nomes_de_api_de_nuvem():
    with pytest.raises(TranscriptionError):
        TR.resolve_model("whisper-1")
