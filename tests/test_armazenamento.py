"""Armazenamento compartilhado com o bot: arquivos JSON (reais, em disco) e Postgres (conexão falsa)."""
import contextlib
import json
import threading

import psycopg
import pytest

from cortador.compartilhado.armazenamento import Armazenamento, ArmazenamentoIndisponivel, ChaveInexistente


# ====================================================================== arquivos JSON do bot

@pytest.fixture
def pasta(tmp_path):
    return tmp_path / "bot"


def _gravar(pasta, chave, dados):
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / f"{chave}.json").write_text(json.dumps(dados), encoding="utf-8")


def test_sem_banco_nem_pasta_nao_esta_ativo():
    a = Armazenamento()
    assert not a.ativo and not a.usa_banco and a.descricao == "não configurado"
    with pytest.raises(ArmazenamentoIndisponivel):
        a.carregar("x", {})
    with pytest.raises(ArmazenamentoIndisponivel):
        a.atualizar("x", {}, lambda d: d)


def test_arquivo_carregar_devolve_o_conteudo_ou_o_padrao(pasta):
    a = Armazenamento(pasta=pasta)
    assert a.ativo and not a.usa_banco and str(pasta) in a.descricao
    assert a.carregar("postagens_publicadas", {}) == {}  # ainda não existe
    _gravar(pasta, "postagens_publicadas", {"tmdb:1": {"titulo": "A"}})
    assert a.carregar("postagens_publicadas", {}) == {"tmdb:1": {"titulo": "A"}}
    _gravar(pasta, "outra", [1, 2])  # tipo diferente do padrão
    assert a.carregar("outra", {}) == {}


def test_arquivo_corrompido_vira_o_padrao(pasta):
    pasta.mkdir()
    (pasta / "postagens_publicadas.json").write_text("{não é json", encoding="utf-8")
    assert Armazenamento(pasta=pasta).carregar("postagens_publicadas", {}) == {}


def test_arquivo_atualizar_altera_e_preserva_o_resto(pasta):
    _gravar(pasta, "postagens_publicadas", {"tmdb:1": {"titulo": "A"}})
    a = Armazenamento(pasta=pasta)
    novo = a.atualizar("postagens_publicadas", {}, lambda d: {**d, "tmdb:2": {"titulo": "B"}})
    assert set(novo) == {"tmdb:1", "tmdb:2"}
    assert set(json.loads((pasta / "postagens_publicadas.json").read_text(encoding="utf-8"))) == {"tmdb:1", "tmdb:2"}


def test_arquivo_atualizar_chave_inexistente_nao_cria(pasta):
    a = Armazenamento(pasta=pasta)
    with pytest.raises(ChaveInexistente):
        a.atualizar("postagens_publicadas", {}, lambda d: {**d, "x": 1})
    assert not (pasta / "postagens_publicadas.json").exists()


def test_arquivo_atualizar_cria_quando_pedido(pasta):
    a = Armazenamento(pasta=pasta)
    a.atualizar("comandos_bot", {}, lambda d: {"jobs": [1]}, criar_se_faltar=True)
    assert json.loads((pasta / "comandos_bot.json").read_text(encoding="utf-8")) == {"jobs": [1]}


def test_arquivo_erro_na_funcao_cancela_a_escrita(pasta):
    _gravar(pasta, "postagens_publicadas", {"tmdb:1": 1})
    a = Armazenamento(pasta=pasta)

    def quebra(d):
        raise RuntimeError("falhou")

    with pytest.raises(RuntimeError):
        a.atualizar("postagens_publicadas", {}, quebra)
    assert a.carregar("postagens_publicadas", {}) == {"tmdb:1": 1}
    assert not [p for p in pasta.iterdir() if p.suffix == ".tmp"]


def test_arquivo_escritas_concorrentes_nao_se_perdem(pasta):
    _gravar(pasta, "postagens_publicadas", {})
    a = Armazenamento(pasta=pasta)
    erros = []

    def gravar(i):
        try:
            a.atualizar("postagens_publicadas", {}, lambda d: {**d, f"tmdb:{i}": i})
        except Exception as erro:  # noqa: BLE001
            erros.append(erro)

    threads = [threading.Thread(target=gravar, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not erros
    assert len(a.carregar("postagens_publicadas", {})) == 12  # nenhuma gravação sobrescreveu a outra


def test_testar_le_o_registro(pasta):
    _gravar(pasta, "postagens_publicadas", {})
    Armazenamento(pasta=pasta).testar()
    with pytest.raises(ArmazenamentoIndisponivel):
        Armazenamento().testar()


# ====================================================================== Postgres (conexão falsa)

class _Resultado:
    def __init__(self, linha):
        self._linha = linha

    def fetchone(self):
        return self._linha


class BancoFalso:
    """Imita o `app_state` do bot: linhas guardadas como JSON (o psycopg devolve o JSONB já decodificado)."""

    def __init__(self, linhas=None):
        self.linhas = {k: json.dumps(v) for k, v in (linhas or {}).items()}
        self.sql: list[str] = []
        self.conexoes = 0
        self.falhar_conexao = False


class ConexaoFalsa:
    def __init__(self, banco: BancoFalso):
        self.banco = banco
        self.fechada = False

    @contextlib.contextmanager
    def transaction(self):
        antes = dict(self.banco.linhas)
        try:
            yield
        except BaseException:
            self.banco.linhas = antes  # rollback
            raise

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.banco.sql.append(sql)
        if sql.startswith("SELECT value FROM app_state"):
            bruto = self.banco.linhas.get(params[0])
            return _Resultado(None if bruto is None else (json.loads(bruto),))
        if sql.startswith("INSERT INTO app_state"):
            self.banco.linhas.setdefault(params[0], params[1])
        elif sql.startswith("UPDATE app_state"):
            self.banco.linhas[params[1]] = params[0]
        return _Resultado(None)

    def close(self):
        self.fechada = True


def _com_banco(banco: BancoFalso) -> Armazenamento:
    a = Armazenamento(database_url="postgresql://falso")

    def conectar():
        if banco.falhar_conexao:
            raise ArmazenamentoIndisponivel("Não consegui conectar ao banco do bot: recusado")
        banco.conexoes += 1
        return ConexaoFalsa(banco)

    a._conectar = conectar
    return a


def test_banco_carregar():
    banco = BancoFalso({"postagens_publicadas": {"tmdb:1": {"titulo": "A"}}, "lista": [1]})
    a = _com_banco(banco)
    assert a.usa_banco and a.descricao == "Postgres do bot"
    assert a.carregar("postagens_publicadas", {}) == {"tmdb:1": {"titulo": "A"}}
    assert a.carregar("ausente", {}) == {}
    assert a.carregar("lista", {}) == {}  # tipo diferente do padrão


def test_banco_define_os_mesmos_limites_de_tempo_do_bot():
    banco = BancoFalso({"k": {}})
    _com_banco(banco).carregar("k", {})
    assert "SET LOCAL statement_timeout = '5s'" in banco.sql and "SET LOCAL lock_timeout = '3s'" in banco.sql


def test_banco_cria_a_tabela_uma_vez_so():
    banco = BancoFalso({"k": {}})
    a = _com_banco(banco)
    a.carregar("k", {})
    a.carregar("k", {})
    assert sum(1 for s in banco.sql if s.startswith("CREATE TABLE IF NOT EXISTS app_state")) == 1


def test_banco_atualizar_usa_for_update_e_grava():
    banco = BancoFalso({"postagens_publicadas": {"tmdb:1": 1}})
    a = _com_banco(banco)
    novo = a.atualizar("postagens_publicadas", {}, lambda d: {**d, "tmdb:2": 2})
    assert novo == {"tmdb:1": 1, "tmdb:2": 2}
    assert json.loads(banco.linhas["postagens_publicadas"]) == {"tmdb:1": 1, "tmdb:2": 2}
    assert any("FOR UPDATE" in s for s in banco.sql)


def test_banco_atualizar_chave_inexistente_nao_cria():
    banco = BancoFalso()
    with pytest.raises(ChaveInexistente):
        _com_banco(banco).atualizar("postagens_publicadas", {}, lambda d: {**d, "x": 1})
    assert "postagens_publicadas" not in banco.linhas


def test_banco_atualizar_cria_quando_pedido():
    banco = BancoFalso()
    _com_banco(banco).atualizar("comandos_bot", {}, lambda d: {"jobs": [1]}, criar_se_faltar=True)
    assert json.loads(banco.linhas["comandos_bot"]) == {"jobs": [1]}


def test_banco_erro_na_funcao_faz_rollback():
    banco = BancoFalso({"postagens_publicadas": {"tmdb:1": 1}})

    def quebra(d):
        raise RuntimeError("duplicado")

    with pytest.raises(RuntimeError):
        _com_banco(banco).atualizar("postagens_publicadas", {}, quebra)
    assert json.loads(banco.linhas["postagens_publicadas"]) == {"tmdb:1": 1}
    assert not any(s.startswith("UPDATE") for s in banco.sql)


def test_banco_fora_do_ar_vira_indisponibilidade():
    banco = BancoFalso({"k": {}})
    banco.falhar_conexao = True
    a = _com_banco(banco)
    with pytest.raises(ArmazenamentoIndisponivel, match="recusado"):
        a.carregar("k", {})
    with pytest.raises(ArmazenamentoIndisponivel):
        a.testar()


def test_banco_erro_de_sql_vira_indisponibilidade_e_fecha_a_conexao():
    banco = BancoFalso({"k": {}})
    a = _com_banco(banco)
    conexoes = []
    original = a._conectar

    def conectar():
        c = original()
        c.execute = lambda *args, **kw: (_ for _ in ()).throw(psycopg.OperationalError("statement timeout"))
        conexoes.append(c)
        return c

    a._conectar = conectar
    with pytest.raises(ArmazenamentoIndisponivel, match="statement timeout"):
        a.carregar("k", {})
    assert conexoes[0].fechada


def test_banco_fecha_a_conexao_depois_de_cada_operacao():
    banco = BancoFalso({"k": {}})
    a = _com_banco(banco)
    conexoes = []
    original = a._conectar
    a._conectar = lambda: conexoes.append(original()) or conexoes[-1]
    a.carregar("k", {})
    a.atualizar("k", {}, lambda d: d)
    assert len(conexoes) == 2 and all(c.fechada for c in conexoes)


def test_url_em_branco_nao_usa_banco():
    assert not Armazenamento("   ").usa_banco
