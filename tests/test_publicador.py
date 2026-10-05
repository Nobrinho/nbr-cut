"""Cortador: ordem de envio, retomada, cancelamento, registro e checagem de duplicidade
(tudo com Enviador/Registro falsos: nada de Telegram nem de banco)."""
import asyncio
import json
from dataclasses import asdict
from datetime import datetime

import pytest

from cortador import jobs, publicador, registro
from cortador.jobs import Job, RepositorioDeJobs
from cortador.publicador import Evento, PublicacaoCancelada, Publicador, legenda_ref, montar_legenda, ordem_de_envio


def _dados_tmdb(sinopse="Resumo curto.", duracao=166):
    return {
        "tmdb_id": 693134, "titulo": "Duna: Parte 2", "titulo_original": "Dune: Part Two", "ano": "2024",
        "duracao": duracao, "nota": 8.4, "classificacao": "14", "generos": ["Ficção científica"],
        "colecao": "Duna", "pais": "EUA", "diretor": "Denis Villeneuve", "estudio": "Legendary",
        "elenco": [("Timothée Chalamet", "https://x/tc.jpg"), ("Zendaya", None)],
        "poster_url": "https://x/p.jpg", "backdrop_url": "https://x/b.jpg", "trailer_url": None,
        "tags": ["deserto"], "sinopse": sinopse,
    }


class EnviadorFalso:
    def __init__(self, falhar_na_chamada: int | None = None):
        self.chamadas: list[tuple] = []
        self.apagadas: list[int] = []
        self.existentes: set[int] | None = None  # None = todos existem
        self._id = 1000
        self._falhar = falhar_na_chamada
        self._n = 0

    def _novo_id(self) -> int:
        self._id += 1
        return self._id

    async def enviar_parte(self, parte, caminho, legenda, como_video, progresso, duracao_s):
        self._n += 1
        if self._falhar == self._n:
            self._falhar = None
            raise ConnectionError("rede caiu")
        progresso(parte.tamanho // 2, parte.tamanho)
        progresso(parte.tamanho, parte.tamanho)
        mid = self._novo_id()
        self.chamadas.append(("parte", parte.indice, legenda, como_video, duracao_s, mid))
        return mid

    async def enviar_texto(self, texto):
        mid = self._novo_id()
        self.chamadas.append(("texto", texto, mid))
        return mid

    async def apagar(self, ids):
        self.apagadas.extend(ids)

    async def existem(self, ids):
        return set(ids) if self.existentes is None else set(ids) & self.existentes


class RegistroFalso:
    def __init__(self, existente=None):
        self.existente = existente
        self.registrados: list[tuple] = []
        self.indices: list[str] = []

    def ja_publicado(self, tmdb_id):
        return self.existente

    def registrar(self, dados_tmdb, nome_arquivo, message_ids, total_partes):
        self.registrados.append((dados_tmdb["tmdb_id"], nome_arquivo, list(message_ids), total_partes))
        return registro.chave_filme(dados_tmdb["tmdb_id"])

    def enfileirar_indice(self, motivo):
        self.indices.append(motivo)
        return "enfileirado"


@pytest.fixture
def ambiente(tmp_path):
    arquivo = tmp_path / "origem.mkv"
    arquivo.write_bytes(bytes(range(256)) * 4)  # 1024 bytes

    def criar_job(maximo=300, dados=None):
        return Job.novo(
            str(arquivo), nome_base="Duna - Parte 2 (2024) [2160p WEB-DL].mkv",
            dados_tmdb=dados or _dados_tmdb(), audio="Dual", qualidade="2160p, WEB-DL, DUAL",
            canal="-100123", maximo=maximo,
        )

    return {
        "arquivo": arquivo,
        "criar_job": criar_job,
        "repo": RepositorioDeJobs(tmp_path / "jobs"),
    }


def _publicador(ambiente, enviador=None, reg=None, eventos=None):
    enviador = enviador or EnviadorFalso()
    reg = reg or RegistroFalso()
    emitir = (lambda e: eventos.append(e)) if eventos is not None else (lambda e: None)
    return Publicador(enviador, reg, ambiente["repo"], emitir), enviador, reg


def _rodar(corotina):
    return asyncio.run(corotina)


def _dados_com_sinopse_longa():
    return _dados_tmdb(sinopse="palavra " * 400)


# --------------------------------------------------------------------------- legenda / ordem

def test_legenda_curta_cabe_na_midia_e_a_longa_nao(ambiente):
    _, cabe = montar_legenda(ambiente["criar_job"]())
    assert cabe is True
    texto, cabe = montar_legenda(ambiente["criar_job"](dados=_dados_com_sinopse_longa()))
    assert cabe is False and len(texto) > 1024


def test_ordem_de_envio_sem_texto_vai_da_ultima_parte_para_a_primeira(ambiente):
    job = ambiente["criar_job"]()
    passos = ordem_de_envio(job, texto_separado=False)
    assert [(p, parte.indice) for p, parte in passos] == [("parte", 4), ("parte", 3), ("parte", 2), ("parte", 1)]


def test_ordem_de_envio_com_texto_poe_o_texto_logo_antes_da_parte_1(ambiente):
    job = ambiente["criar_job"]()
    passos = ordem_de_envio(job, texto_separado=True)
    nomes = [(p, parte.indice if parte else None) for p, parte in passos]
    assert nomes == [("parte", 4), ("parte", 3), ("parte", 2), ("texto", None), ("parte", 1)]


def test_ordem_de_envio_so_traz_o_que_falta(ambiente):
    job = ambiente["criar_job"]()
    job.partes[3].message_id = 11   # parte 4 já foi
    job.partes[2].message_id = 12   # parte 3 já foi
    job.texto_message_id = 13
    passos = ordem_de_envio(job, texto_separado=True)
    assert [(p, parte.indice) for p, parte in passos] == [("parte", 2), ("parte", 1)]


# --------------------------------------------------------------------------- fluxo feliz

def test_envia_na_ordem_com_texto_separado_e_registra_em_ordem_logica(ambiente):
    job = ambiente["criar_job"](dados=_dados_com_sinopse_longa())
    pub, env, reg = _publicador(ambiente)
    chave = _rodar(pub.executar(job))

    tipos = [(c[0], c[1]) for c in env.chamadas]
    assert tipos == [("parte", 4), ("parte", 3), ("parte", 2), ("texto", env.chamadas[3][1]), ("parte", 1)]
    parte1 = env.chamadas[4]
    assert "TMDB: 693134" in parte1[2] and "Sinopse" not in parte1[2]
    assert all(c[2] is None for c in env.chamadas[:3])      # demais partes sem legenda
    assert all(c[3] is False for c in env.chamadas if c[0] == "parte")  # documento, não vídeo
    # ordem LÓGICA no registro: [texto, p1, p2, p3, p4] (as ids foram enviadas fora de ordem)
    ids = {c[1]: c[-1] for c in env.chamadas if c[0] == "parte"}
    texto_id = env.chamadas[3][2]
    assert reg.registrados == [(693134, job.nome_base, [texto_id, ids[1], ids[2], ids[3], ids[4]], 4)]
    assert chave == "tmdb:693134"
    assert reg.indices and "Duna" in reg.indices[0]


def test_legenda_que_cabe_vai_inteira_na_parte_1_e_nao_ha_texto(ambiente):
    job = ambiente["criar_job"]()
    pub, env, reg = _publicador(ambiente)
    _rodar(pub.executar(job))
    assert [c[0] for c in env.chamadas] == ["parte"] * 4
    legenda_parte_1 = [c for c in env.chamadas if c[1] == 1][0][2]
    assert legenda_parte_1.startswith("Título: Duna: Parte 2") and "Sinopse: Resumo curto." in legenda_parte_1
    ids = {c[1]: c[-1] for c in env.chamadas}
    assert reg.registrados[0][2] == [ids[1], ids[2], ids[3], ids[4]]


def test_arquivo_unico_sobe_como_video_com_a_duracao_do_tmdb(ambiente):
    job = ambiente["criar_job"](maximo=5_000)  # cabe numa parte
    assert job.total_partes == 1
    pub, env, reg = _publicador(ambiente)
    _rodar(pub.executar(job))
    (chamada,) = env.chamadas
    assert chamada[0] == "parte" and chamada[3] is True      # como_video
    assert chamada[4] == 166 * 60                            # duração em segundos, do TMDB
    assert reg.registrados[0][3] == 1


def test_arquivo_unico_com_legenda_longa_manda_texto_e_depois_o_video(ambiente):
    job = ambiente["criar_job"](maximo=5_000, dados=_dados_com_sinopse_longa())
    pub, env, _ = _publicador(ambiente)
    _rodar(pub.executar(job))
    assert [c[0] for c in env.chamadas] == ["texto", "parte"]


def test_job_e_removido_ao_concluir_e_emite_os_eventos(ambiente):
    job = ambiente["criar_job"]()
    eventos: list[Evento] = []
    pub, _, _ = _publicador(ambiente, eventos=eventos)
    _rodar(pub.executar(job))
    assert ambiente["repo"].carregar(job.id) is None
    tipos = [e.tipo for e in eventos]
    assert tipos[0] == "inicio" and tipos[-1] == "concluido"
    assert tipos.index("registrado") < tipos.index("indice") < tipos.index("concluido")
    assert tipos.count("parte_ok") == 4
    progresso = [e for e in eventos if e.tipo == "progresso"]
    assert progresso and progresso[-1].feito == progresso[-1].total


def test_progresso_traz_a_parte_e_o_total(ambiente):
    job = ambiente["criar_job"]()
    eventos: list[Evento] = []
    pub, _, _ = _publicador(ambiente, eventos=eventos)
    _rodar(pub.executar(job))
    primeiro = next(e for e in eventos if e.tipo == "parte_inicio")
    assert primeiro.parte == 4 and primeiro.total_partes == 4 and primeiro.total == job.partes[3].tamanho


# --------------------------------------------------------------------------- falha e retomada

def test_falha_no_meio_deixa_o_job_retomavel_e_nao_registra(ambiente):
    job = ambiente["criar_job"]()
    pub, env, reg = _publicador(ambiente, EnviadorFalso(falhar_na_chamada=3))
    with pytest.raises(ConnectionError):
        _rodar(pub.executar(job))
    salvo = ambiente["repo"].carregar(job.id)
    assert salvo.estado == jobs.ERRO and "rede caiu" in salvo.erro
    assert sum(1 for p in salvo.partes if p.message_id) == 2
    assert reg.registrados == [] and reg.indices == []


def test_retomar_envia_so_o_que_falta_e_registra_tudo(ambiente):
    job = ambiente["criar_job"]()
    pub, env, reg = _publicador(ambiente, EnviadorFalso(falhar_na_chamada=3))
    with pytest.raises(ConnectionError):
        _rodar(pub.executar(job))

    retomado = ambiente["repo"].carregar(job.id)
    pub2, env2, reg2 = _publicador(ambiente, reg=reg)
    _rodar(pub2.executar(retomado))
    # a retomada só subiu as 2 partes que faltavam (a 1 e a 2), não as que já estavam no canal
    assert sorted(c[1] for c in env2.chamadas) == [1, 2]
    assert len(reg.registrados) == 1 and len(reg.registrados[0][2]) == 4
    assert ambiente["repo"].carregar(job.id) is None


def test_retomar_reenvia_parte_apagada_do_canal(ambiente):
    job = ambiente["criar_job"]()
    pub, env, reg = _publicador(ambiente, EnviadorFalso(falhar_na_chamada=3))
    with pytest.raises(ConnectionError):
        _rodar(pub.executar(job))
    retomado = ambiente["repo"].carregar(job.id)
    apagado = next(p for p in retomado.partes if p.message_id)  # uma das 2 enviadas sumiu
    sobreviventes = {p.message_id for p in retomado.partes if p.message_id and p is not apagado}

    pub2, env2, _ = _publicador(ambiente, reg=reg)
    env2.existentes = sobreviventes
    _rodar(pub2.executar(retomado))
    assert apagado.indice in [c[1] for c in env2.chamadas]


def test_retomada_nao_checa_duplicidade_do_que_ja_comecou(ambiente):
    job = ambiente["criar_job"]()
    pub, _, reg = _publicador(ambiente, EnviadorFalso(falhar_na_chamada=2))
    with pytest.raises(ConnectionError):
        _rodar(pub.executar(job))
    reg.existente = {"tmdb_id": 693134}  # apareceu no registro (ex.: outro processo)
    retomado = ambiente["repo"].carregar(job.id)
    pub2, _, _ = _publicador(ambiente, reg=reg)
    _rodar(pub2.executar(retomado))  # não levanta JaPublicado: o job já tinha envios


# --------------------------------------------------------------------------- cancelar

def test_cancelar_para_entre_partes_e_guarda_o_estado(ambiente):
    job = ambiente["criar_job"]()
    pub, env, reg = _publicador(ambiente)
    estado = {"n": 0}

    def cancelado():
        estado["n"] += 1
        return estado["n"] > 2  # deixa subir 2 partes e cancela

    with pytest.raises(PublicacaoCancelada):
        _rodar(pub.executar(job, cancelado))
    assert len(env.chamadas) == 2
    assert reg.registrados == []
    assert ambiente["repo"].carregar(job.id) is not None


def test_descartar_apaga_o_que_subiu_e_esquece_o_job(ambiente):
    job = ambiente["criar_job"]()
    pub, env, _ = _publicador(ambiente)
    estado = {"n": 0}

    def cancelado():
        estado["n"] += 1
        return estado["n"] > 2

    with pytest.raises(PublicacaoCancelada):
        _rodar(pub.executar(job, cancelado))
    enviados = job.message_ids_enviados
    _rodar(pub.descartar(job))
    assert sorted(env.apagadas) == sorted(enviados) and len(enviados) == 2
    assert ambiente["repo"].carregar(job.id) is None
    assert job.estado == jobs.CANCELADO


# --------------------------------------------------------------------------- duplicidade e arquivo

def test_duplicado_bloqueia_antes_de_subir_qualquer_byte(ambiente):
    job = ambiente["criar_job"]()
    pub, env, reg = _publicador(ambiente, reg=RegistroFalso(existente={"titulo": "Duna", "publicado_em": "2026-10-01"}))
    with pytest.raises(registro.JaPublicado) as erro:
        _rodar(pub.executar(job))
    assert erro.value.chave == "tmdb:693134"
    assert env.chamadas == []


def test_arquivo_que_mudou_nao_e_postado(ambiente):
    job = ambiente["criar_job"]()
    ambiente["arquivo"].write_bytes(b"outro conteudo")
    pub, env, _ = _publicador(ambiente)
    with pytest.raises(ValueError, match="mudou"):
        _rodar(pub.executar(job))
    assert env.chamadas == []


def test_job_concluido_nao_roda_de_novo(ambiente):
    job = ambiente["criar_job"]()
    job.estado = jobs.CONCLUIDO
    pub, _, _ = _publicador(ambiente)
    with pytest.raises(ValueError):
        _rodar(pub.executar(job))


# --------------------------------------------------------------------------- jobs em disco

def test_job_vai_e_volta_do_disco_sem_perder_nada(ambiente):
    job = ambiente["criar_job"]()
    job.partes[0].message_id = 77
    job.texto_message_id = 76
    ambiente["repo"].salvar(job)
    lido = ambiente["repo"].carregar(job.id)
    # tuplas do elenco viram listas no JSON: compara a forma serializável
    assert asdict(lido) == json.loads(json.dumps(asdict(job)))
    assert lido.ids_em_ordem_logica() == [76, 77]


def test_pendentes_lista_so_o_que_nao_terminou_e_ignora_arquivo_corrompido(ambiente):
    a = ambiente["criar_job"]()
    b = ambiente["criar_job"]()
    b.estado = jobs.CONCLUIDO
    ambiente["repo"].salvar(a)
    ambiente["repo"].salvar(b)
    (ambiente["repo"].pasta / "lixo.json").write_text("{não é json")
    assert [j.id for j in ambiente["repo"].pendentes()] == [a.id]


def test_ids_em_ordem_logica_ignora_o_que_nao_foi_enviado(ambiente):
    job = ambiente["criar_job"]()
    job.partes[1].message_id = 5
    assert job.ids_em_ordem_logica() == [5]
    assert job.tem_envios


def test_dados_do_tmdb_com_tuplas_sobrevivem_ao_json(ambiente):
    job = ambiente["criar_job"]()
    ambiente["repo"].salvar(job)
    lido = ambiente["repo"].carregar(job.id)
    # o elenco vira lista no JSON; a legenda continua igual
    assert montar_legenda(lido)[0] == montar_legenda(job)[0]


def test_job_cria_plano_coerente_com_o_arquivo(ambiente):
    job = ambiente["criar_job"]()
    assert job.tamanho_origem == 1024 and job.total_partes == 4
    assert sum(p.tamanho for p in job.partes) == 1024
    assert [p.nome for p in job.partes][0].endswith(".mkv.part01of04")
    assert not job.arquivo_mudou()
