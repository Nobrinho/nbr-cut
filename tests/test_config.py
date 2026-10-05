"""Configuração própria do app: arquivo, segredos protegidos, importação do .env do bot e validação."""
import json

from cortador import config, divisao
from cortador.compartilhado.armazenamento import Armazenamento

MIB = 1024 * 1024


def _completa(**extra):
    campos = dict(
        api_id=12345, api_hash="hash-secreto", tmdb_key="chave-tmdb", canal_destino="-100777",
        canal_teste="-100999", database_url="postgresql://u:senha@localhost:5432/db",
    )
    campos.update(extra)
    return config.Configuracao(**campos)


def test_arquivo_ausente_ou_corrompido_da_configuracao_vazia(tmp_path):
    vazia = config.carregar(tmp_path / "nao-existe.json")
    assert (vazia.api_id, vazia.api_hash, vazia.tmdb_key, vazia.canal_destino, vazia.database_url) == (0, "", "", "", "")
    (tmp_path / "ruim.json").write_text("{lixo", encoding="utf-8")
    assert config.carregar(tmp_path / "ruim.json").api_id == 0
    (tmp_path / "lista.json").write_text("[1,2]", encoding="utf-8")
    assert config.carregar(tmp_path / "lista.json").api_hash == ""


def test_salvar_e_carregar_devolvem_o_mesmo(tmp_path):
    arquivo = tmp_path / "config.json"
    cfg = _completa(premium=True, tamanho_parte=3000 * MIB, modo_teste=True, pasta_registro=r"C:\bot")
    config.salvar(cfg, arquivo)
    lido = config.carregar(arquivo)
    for campo in ("api_id", "api_hash", "tmdb_key", "canal_destino", "canal_teste", "database_url",
                  "pasta_registro", "premium", "tamanho_parte", "modo_teste"):
        assert getattr(lido, campo) == getattr(cfg, campo), campo
    assert lido.avisos == []


def test_segredos_nao_ficam_em_texto_no_arquivo(tmp_path):
    arquivo = tmp_path / "config.json"
    config.salvar(_completa(), arquivo)
    bruto = arquivo.read_text(encoding="utf-8")
    for segredo in ("hash-secreto", "chave-tmdb", "senha", "postgresql://"):
        assert segredo not in bruto
    # o que não é segredo continua legível (facilita suporte)
    assert "-100777" in bruto and "12345" in bruto


def test_campo_vazio_fica_vazio_sem_protecao(tmp_path):
    arquivo = tmp_path / "config.json"
    config.salvar(config.Configuracao(api_id=1), arquivo)
    dados = json.loads(arquivo.read_text(encoding="utf-8"))
    assert dados["api_hash"] == "" and dados["database_url"] == ""


def test_segredo_ilegivel_vira_aviso_e_o_resto_carrega(tmp_path):
    arquivo = tmp_path / "config.json"
    config.salvar(_completa(), arquivo)
    dados = json.loads(arquivo.read_text(encoding="utf-8"))
    dados["api_hash"] = "dpapi:AAAA-corrompido"
    arquivo.write_text(json.dumps(dados), encoding="utf-8")
    lido = config.carregar(arquivo)
    assert lido.api_hash == "" and lido.tmdb_key == "chave-tmdb"
    assert any("api_hash" in aviso for aviso in lido.avisos)


def test_tamanho_padrao_depende_do_premium(tmp_path):
    arquivo = tmp_path / "config.json"
    arquivo.write_text(json.dumps({"premium": False}), encoding="utf-8")
    assert config.carregar(arquivo).tamanho_parte == divisao.TAMANHO_PADRAO
    arquivo.write_text(json.dumps({"premium": True}), encoding="utf-8")
    assert config.carregar(arquivo).tamanho_parte == divisao.TAMANHO_PADRAO_PREMIUM


def test_salvar_nao_deixa_temporario(tmp_path):
    config.salvar(_completa(), tmp_path / "pasta-nova" / "config.json")
    assert [p.name for p in (tmp_path / "pasta-nova").iterdir()] == ["config.json"]


# --------------------------------------------------------------------------- importar do bot

def test_importar_do_env_do_bot(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_API_ID=123\nTELEGRAM_API_HASH=abc\nTMDB_API_KEY=k\nCANAL_DESTINO_ID=-100777\n"
        "DATABASE_URL=postgresql://x\nBOT_TOKEN=nao-importa\n", encoding="utf-8")
    achados = config.importar_do_env(env)
    assert achados == {"api_id": 123, "api_hash": "abc", "tmdb_key": "k", "canal_destino": "-100777",
                       "database_url": "postgresql://x"}
    assert "BOT_TOKEN" not in str(achados)  # só o que o app usa


def test_importar_aceita_a_url_antiga_do_neon_e_ignora_o_que_falta(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NEON_DATABASE_URL=postgresql://neon\nTELEGRAM_API_ID=nao-numero\n", encoding="utf-8")
    assert config.importar_do_env(env) == {"database_url": "postgresql://neon"}


def test_importar_de_arquivo_inexistente_devolve_vazio(tmp_path):
    assert config.importar_do_env(tmp_path / "nao-existe.env") == {}


def test_aplicar_copia_so_campos_conhecidos():
    cfg = config.aplicar(config.Configuracao(), {"api_id": 9, "tmdb_key": "k", "campo_que_nao_existe": 1})
    assert cfg.api_id == 9 and cfg.tmdb_key == "k" and not hasattr(cfg, "campo_que_nao_existe")


# --------------------------------------------------------------------------- validação

def test_problemas_da_configuracao_vazia():
    problemas = config.Configuracao().problemas()
    assert any("api_id" in p for p in problemas)
    assert any("TMDB" in p for p in problemas)
    assert any("canal de destino" in p for p in problemas)


def test_configuracao_completa_nao_tem_problemas():
    assert _completa().problemas() == []


def test_modo_teste_troca_o_canal_ativo():
    cfg = _completa()
    assert cfg.canal_ativo == "-100777"
    cfg.modo_teste = True
    assert cfg.canal_ativo == "-100999"
    cfg.canal_teste = ""
    assert any("canal de teste" in p.lower() for p in cfg.problemas())


def test_tamanho_acima_do_limite_da_conta_e_problema():
    cfg = _completa(tamanho_parte=3900 * MIB, premium=False)
    assert any("limite" in p for p in cfg.problemas())
    cfg.premium = True
    assert not any("limite" in p for p in cfg.problemas())


def test_armazenamento_vem_da_configuracao():
    assert not _completa(database_url="").armazenamento().ativo
    assert _completa().armazenamento().usa_banco
    a = config.Configuracao(pasta_registro=r"C:\bot").armazenamento()
    assert a.ativo and not a.usa_banco


def test_canal_como_entidade():
    assert config.canal_como_entidade("-1001234") == -1001234
    assert config.canal_como_entidade(" @meucanal ") == "@meucanal"
