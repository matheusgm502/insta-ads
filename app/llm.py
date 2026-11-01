"""Camada de LLM: um único ponto de entrada, qualquer fornecedor.

O resto do app chama `run(tarefa, prompt, ...)` e nunca importa um SDK de fornecedor.
O modelo vem da configuração, no formato `fornecedor:modelo` do Pydantic AI
(anthropic:, openai:, deepseek:, zai:, openrouter:, ...), então trocar de fornecedor
é mudar uma variável de ambiente, sem tocar no código:

    APP_MODEL=anthropic:claude-sonnet-5-5          # padrão para todas as tarefas
    APP_MODEL_COMPLIANCE=anthropic:claude-opus-5-5 # só para a tarefa "compliance"

Chaves, no .env (nunca no código): ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY,
ZAI_API_KEY.

    python3 -m app.llm --check                                 # testa o modelo configurado
    python3 -m app.llm --check --models anthropic:claude-haiku-5-5,deepseek:deepseek-flash
"""

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

ROOT = Path(__file__).resolve().parent.parent
PRICES_PATH = Path(__file__).with_name("model_prices.json")
DEFAULT_MODEL = "anthropic:claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 4096


def load_env(path=ROOT / ".env"):
    """Carrega o .env sem sobrescrever variáveis já definidas."""
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def model_for(task):
    """Modelo da tarefa: APP_MODEL_<TAREFA>, senão APP_MODEL, senão o padrão."""
    return os.environ.get(f"APP_MODEL_{task.upper()}") or os.environ.get("APP_MODEL") or DEFAULT_MODEL


def estimate_cost(model_name, input_tokens, output_tokens):
    """Custo estimado em USD pela tabela de preços; None se o modelo não está nela."""
    prices = json.loads(PRICES_PATH.read_text())
    if model_name not in prices or model_name == "_meta":
        return None
    price_in, price_out = prices[model_name]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


@dataclass
class LLMResult:
    output: object
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


def run(task, prompt, *, instructions="", output_type=str, model=None, max_tokens=DEFAULT_MAX_TOKENS):
    """Executa uma tarefa de texto e devolve o resultado com uso e custo estimado.

    output_type: `str` (padrão) ou uma classe Pydantic. Com uma classe, a resposta já vem
    validada e o Pydantic AI repete a chamada se o modelo errar o formato.
    model: sobrescreve a configuração (string `fornecedor:modelo` ou objeto Model).
    Não passamos temperature/top_p: vários modelos novos rejeitam esses parâmetros.
    """
    load_env()
    chosen = model or model_for(task)
    agent = Agent(chosen, output_type=output_type, instructions=instructions or None)
    result = agent.run_sync(prompt, model_settings={"max_tokens": max_tokens})
    usage = result.usage
    name = chosen if isinstance(chosen, str) else getattr(chosen, "model_name", str(chosen))
    return LLMResult(result.output, name, usage.input_tokens, usage.output_tokens,
                     estimate_cost(name, usage.input_tokens, usage.output_tokens))


# --- verificação de chaves e modelos ----------------------------------------

class _Check(BaseModel):
    ok: bool
    palavra: str


def check(models):
    """Chama cada modelo duas vezes (texto e saída estruturada) e imprime o resultado."""
    failures = 0
    for name in models:
        for label, kind, prompt in (("texto", str, "Responda apenas a palavra: ok"),
                                    ("estruturado", _Check, "Devolva ok=true e palavra='ok'.")):
            try:
                r = run("check", prompt, output_type=kind, model=name, max_tokens=256)
                cost = f"US$ {r.cost_usd:.5f}" if r.cost_usd is not None else "preço desconhecido"
                print(f"OK    {name} [{label}]: {str(r.output)[:60]!r} "
                      f"({r.input_tokens}+{r.output_tokens} tokens, {cost})")
            except Exception as e:  # mostra o erro sem derrubar os outros modelos
                failures += 1
                print(f"FALHA {name} [{label}]: {type(e).__name__}: {str(e)[:200]}")
    return failures


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="testa chave e formato de cada modelo")
    ap.add_argument("--models", help="lista separada por vírgula (padrão: o modelo configurado)")
    args = ap.parse_args()
    load_env()
    if not args.check:
        ap.print_help()
        return
    models = args.models.split(",") if args.models else [model_for("check")]
    raise SystemExit(1 if check(models) else 0)


if __name__ == "__main__":
    main()
