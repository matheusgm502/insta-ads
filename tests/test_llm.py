"""Testes da camada de LLM. Sem rede e sem chaves: usam o modelo de teste do Pydantic AI."""

from pydantic import BaseModel
from pydantic_ai.models.test import TestModel

from app import llm


class Ficha(BaseModel):
    titulo: str
    pontos: list[str]


def test_text_task_returns_usage(monkeypatch):
    r = llm.run("analise", "Resuma.", model=TestModel(custom_output_text="resumo pronto"))
    assert r.output == "resumo pronto"
    assert r.input_tokens > 0 and r.output_tokens > 0
    assert r.cost_usd is None  # modelo de teste não tem preço


def test_structured_output_is_validated_into_the_model_class():
    r = llm.run("brief", "Monte a ficha.", output_type=Ficha, model=TestModel())
    assert isinstance(r.output, Ficha)


def test_model_selection_per_task_then_global_then_default(monkeypatch):
    for var in ("APP_MODEL", "APP_MODEL_BRIEF"):
        monkeypatch.delenv(var, raising=False)
    assert llm.model_for("brief") == llm.DEFAULT_MODEL
    monkeypatch.setenv("APP_MODEL", "deepseek:deepseek-flash")
    assert llm.model_for("brief") == "deepseek:deepseek-flash"
    monkeypatch.setenv("APP_MODEL_BRIEF", "zai:glm-5.3")
    assert llm.model_for("brief") == "zai:glm-5.3"
    assert llm.model_for("outra") == "deepseek:deepseek-flash"


def test_cost_estimate_uses_the_price_table():
    # 1 milhão de tokens de entrada e 1 milhão de saída no Sonnet 5.5 = 2 + 10 dólares
    assert llm.estimate_cost("anthropic:claude-sonnet-5-5", 1_000_000, 1_000_000) == 12.0
    assert llm.estimate_cost("deepseek:deepseek-flash", 20_000, 3_000) < 0.01
    assert llm.estimate_cost("fornecedor:desconhecido", 100, 100) is None
    assert llm.estimate_cost("_meta", 100, 100) is None


def test_env_loader_does_not_override_existing_variables(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comentário\nAPP_TESTE_A=do_arquivo\nAPP_TESTE_B=novo\n")
    monkeypatch.setenv("APP_TESTE_A", "do_ambiente")
    monkeypatch.delenv("APP_TESTE_B", raising=False)
    llm.load_env(env)
    assert llm.os.environ["APP_TESTE_A"] == "do_ambiente"
    assert llm.os.environ["APP_TESTE_B"] == "novo"
    monkeypatch.delenv("APP_TESTE_B")


def test_every_priced_model_uses_a_known_provider_prefix():
    prices = llm.json.loads(llm.PRICES_PATH.read_text())
    prefixes = {k.split(":")[0] for k in prices if k != "_meta"}
    assert prefixes <= {"anthropic", "openai", "deepseek", "zai"}
