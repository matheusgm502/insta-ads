"""Triagem e ranking de candidatos a impulsionamento. Só dados sintéticos."""

from datetime import datetime, timedelta, timezone

from app import analysis, boost_candidates as bc
from app import db as appdb

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


def make_post(caption="", speech=None, screen_text=None):
    return analysis.Post(id="x", date=NOW, kind="REELS", permalink="l", caption=caption,
                         metrics={"reach": 1}, speech=speech, screen_text=screen_text)


def codes(post):
    return {f["code"] for f in bc.triage(post)}


def test_clean_text_has_no_alerts():
    assert codes(make_post("Hoje vou falar sobre limites na rotina.", "Conversar ajuda a organizar o que a gente sente.")) == set()


def test_price_and_promotion_are_flagged_from_caption_or_speech():
    assert "preco_ou_promocao" in codes(make_post("Primeira sessão grátis!"))
    assert "preco_ou_promocao" in codes(make_post("", "Agora com desconto para novos pacientes"))
    assert "preco_ou_promocao" in codes(make_post("Sessão por R$ 120"))


def test_promise_testimonial_and_sensationalism_are_flagged():
    assert "promessa_de_resultado" in codes(make_post("Garanto que você vai melhorar"))
    assert "promessa_de_resultado" in codes(make_post("Resultado em 100% dos casos"))
    assert "depoimento_ou_caso" in codes(make_post("", "Uma paciente me contou que..."))
    assert "sensacionalismo" in codes(make_post("Isso é chocante, não ignore"))


def test_words_that_merely_contain_a_trigger_are_not_flagged():
    # "procura" contém "cura"; "gratidão" começa como "gratis"; "pacientemente" contém "paciente"
    assert codes(make_post("Quem procura acolhimento, gratidão e vai pacientemente") ) == set()


def test_viewer_condition_rule_flags_implied_diagnosis_but_not_plain_description():
    assert "condicao_do_espectador" in codes(make_post("", "Você tem ansiedade e não percebe?"))
    assert "condicao_do_espectador" in codes(make_post("Será que você sofre de depressão?"))
    assert "condicao_do_espectador" not in codes(make_post("Psicoterapia para ansiedade e estresse"))
    assert "condicao_do_espectador" not in codes(make_post("", "A ansiedade é uma emoção comum a todos nós"))


def test_flag_includes_an_excerpt_for_the_human_reviewer():
    flag = bc.triage(make_post("Venha conhecer: primeira sessão grátis para você"))[0]
    assert "grátis" in flag["excerpt"]


def build_db(path):
    db = appdb.connect(path)
    acc = appdb.ensure_account(db, "a", "conta_teste")
    rows = [  # id, dias atrás, alcance, compartilhamentos, texto
        ("alto", 10, 2000, 40, "Fala sem problemas"), ("medio", 20, 800, 10, "Outra fala"),
        ("baixo", 30, 300, 1, "Mais uma"), ("risco", 40, 1800, 35, "Primeira sessão grátis"),
        ("viral", 50, 500_000, 9000, "Fala viral"), ("antigo", 700, 3000, 80, "Fala antiga"),
        ("a", 15, 700, 8, "x"), ("b", 25, 650, 7, "y"), ("c", 35, 600, 6, "z"),
    ]
    for pid, days, reach, shares, text in rows:
        ts = (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        db.execute("INSERT INTO media (id, account_id, media_type, media_product_type, timestamp, permalink, caption) "
                   "VALUES (?, ?, 'VIDEO', 'REELS', ?, ?, '')", (pid, acc, ts, f"https://example.test/{pid}"))
        for metric, value in (("reach", reach), ("shares", shares)):
            db.execute("INSERT INTO media_insights VALUES (?, '2026-11-01', ?, ?)", (pid, metric, value))
        db.execute("INSERT INTO transcripts (media_id, hook, text) VALUES (?, ?, ?)", (pid, text, text))
    db.commit()
    db.close()


def test_ranking_orders_by_score_excludes_outliers_and_old_posts_and_marks_risk(tmp_path):
    build_db(tmp_path / "x.sqlite")
    posts = analysis.load_posts(tmp_path / "x.sqlite")
    ranked, outliers = bc.candidates(posts, days=365, top=10, now=NOW)
    ids = [r["post"].id for r in ranked]
    assert ids[:2] == ["alto", "risco"]            # os melhores primeiro
    assert "viral" not in ids and "antigo" not in ids
    assert [p.id for p in outliers] == ["viral"]
    risk = next(r for r in ranked if r["post"].id == "risco")
    assert [f["code"] for f in risk["flags"]] == ["preco_ou_promocao"]
    assert all(not r["flags"] for r in ranked if r["post"].id != "risco")


def test_report_has_the_warning_and_lists_alerts(tmp_path):
    build_db(tmp_path / "x.sqlite")
    ranked, outliers = bc.candidates(analysis.load_posts(tmp_path / "x.sqlite"), now=NOW)
    text = bc.render(ranked, outliers, 365)
    assert "não substitui" in text.lower() or "Não substitui" in text
    assert "## Alertas da triagem" in text and "preco_ou_promocao" in text
    assert "## Fora da curva" in text


def test_political_topics_are_flagged_but_everyday_words_are_not():
    assert "tema_politico" in codes(make_post("", "Hoje a conversa é sobre eleição e governo"))
    assert "tema_politico" in codes(make_post("A política não deveria entrar no consultório"))
    assert "tema_politico" not in codes(make_post("Escreva com a mão direita e vire à esquerda", "Um presente para você"))


def test_text_that_only_appears_on_screen_is_also_triaged():
    # um Reel sem fala: o conteúdo está no texto sobreposto, que a triagem precisa ler
    assert "preco_ou_promocao" in codes(make_post("Legenda neutra", None, "Primeira sessão grátis para você"))
    assert codes(make_post("Legenda neutra", None, "Respire fundo e recomece")) == set()


def test_screen_text_is_loaded_from_described_frames(tmp_path):
    import json
    db = appdb.connect(tmp_path / "x.sqlite")
    acc = appdb.ensure_account(db, "a")
    ts = NOW.strftime("%Y-%m-%dT%H:%M:%S+0000")
    db.execute("INSERT INTO media (id, account_id, media_type, media_product_type, timestamp) VALUES ('m', ?, 'VIDEO', 'REELS', ?)", (acc, ts))
    db.execute("INSERT INTO media_insights VALUES ('m', '2026-11-01', 'reach', 10)")
    for idx, text in enumerate(["Primeira frase", "Nenhum", "Segunda frase", "Primeira frase"]):
        db.execute("INSERT INTO visual_frames (media_id, idx, description) VALUES ('m', ?, ?)",
                   (idx, json.dumps({"texto_na_tela": text})))
    db.commit(); db.close()
    post = analysis.load_posts(tmp_path / "x.sqlite")[0]
    assert post.screen_text == "Primeira frase Segunda frase"  # sem repetição e sem "Nenhum"
