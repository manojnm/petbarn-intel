"""Typer CLI entrypoint: `petbarn-intel <group> <command>`.

Groups are wired in as their implementations land (see the build order in the
plan). Run `petbarn-intel --help` for the current command tree.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from petbarn_intel.config import get_settings
from petbarn_intel.logging import ensure_configured
from petbarn_intel.store.db import get_connection, init_db

ensure_configured()
console = Console()

app = typer.Typer(help="Petbarn agentic scraping & product intelligence platform.")

db_app = typer.Typer(help="Database utilities.")
scrape_app = typer.Typer(help="Scraping engine: census, seed selection, deep scrape, reviews.")
report_app = typer.Typer(help="Ops reports: coverage, incidents, cost.")
agent_app = typer.Typer(help="Agent / LLM utilities.")
enrich_app = typer.Typer(help="Enrichment: aspect sentiment, embeddings.")

app.add_typer(db_app, name="db")
app.add_typer(scrape_app, name="scrape")
app.add_typer(report_app, name="report")
app.add_typer(agent_app, name="agent")
app.add_typer(enrich_app, name="enrich")


@enrich_app.command("aspects")
def enrich_aspects(
    concurrency: int = typer.Option(4, help="Max concurrent LLM batch calls."),
) -> None:
    """Batch-extract aspect sentiment for every seed product's reviews
    missing one at the current prompt version. Safe to re-run -- only
    processes what's not already covered."""
    from petbarn_intel.enrichment.aspects import run_seed_aspect_enrichment

    init_db(get_connection())
    settings = get_settings()
    if not settings.has_llm_credentials:
        console.print("[red]No LLM credentials configured -- check .env.[/red]")
        raise typer.Exit(1)

    result = asyncio.run(run_seed_aspect_enrichment(concurrency=concurrency))
    console.print(f"[green]Aspect enrichment complete[/green]: {result}")


@enrich_app.command("embeddings")
def enrich_embeddings(
    product_id: str = typer.Option(None, help="Only embed reviews for this product."),
    concurrency: int = typer.Option(25, help="Max concurrent calls (Azure path only)."),
) -> None:
    """Embed every review missing a vector -- Azure `text-embedding-3-large`
    if a deployment is configured, else local fastembed bge-small. Safe to
    re-run."""
    from petbarn_intel.enrichment.embeddings import model_name, run_embedding_batch

    init_db(get_connection())
    console.print(f"[dim]model: {model_name()}[/dim]")
    result = asyncio.run(run_embedding_batch(product_id=product_id, concurrency=concurrency))
    console.print(f"[green]Embedding complete[/green]: {result}")


@agent_app.command("ping")
def agent_ping(
    prompt: str = typer.Option("Say 'ok' and nothing else.", help="Prompt to send."),
    role: str = typer.Option("main", help="'mini' or 'main' -- see Settings.chat_model_id."),
) -> None:
    """One-shot smoke test of the configured Azure/OpenAI chat deployment."""
    from petbarn_intel.agent.llm import chat

    settings = get_settings()
    if not settings.has_llm_credentials:
        console.print("[red]No LLM credentials configured -- check .env.[/red]")
        raise typer.Exit(1)

    console.print(f"[dim]model: {settings.chat_model_id(role)}[/dim]")
    reply = chat([{"role": "user", "content": prompt}], role=role)
    console.print(f"[green]Reply:[/green] {reply}")


@agent_app.command("chat")
def agent_chat(session: str = typer.Option("cli", help="Session id (groups turns for memory).")) -> None:
    """Interactive REPL against the full agent graph (guard -> planner ->
    specialists -> synthesizer -> verifier) -- the same graph the
    Streamlit app calls. Ctrl-D / "exit" to quit."""
    from petbarn_intel.agent.graph import run_turn

    init_db(get_connection())
    settings = get_settings()
    if not settings.has_llm_credentials:
        console.print("[red]No LLM credentials configured -- check .env.[/red]")
        raise typer.Exit(1)

    history: list[tuple[str, str]] = []
    console.print("[dim]petbarn-intel agent chat -- type 'exit' to quit[/dim]")
    while True:
        try:
            question = console.input("[bold cyan]you>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question or question.lower() in {"exit", "quit"}:
            break
        result = asyncio.run(run_turn(question, session_id=session, history=history))
        console.print(f"[bold green]assistant[/bold green] "
                      f"[dim]({result['mode']}, {','.join(result['specialists']) or 'none'})[/dim]:")
        console.print(result["answer"])
        history.append((question, result["answer"]))


@db_app.command("init")
def db_init() -> None:
    """Create/upgrade the SQLite schema (idempotent)."""
    conn = get_connection()
    init_db(conn)
    console.print(f"[green]DB ready at[/green] {get_settings().db_path}")


@db_app.command("status")
def db_status() -> None:
    """Show table row counts."""
    conn = get_connection()
    init_db(conn)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    table = Table(title="Tables")
    table.add_column("table")
    table.add_column("rows", justify="right")
    for r in rows:
        name = r["name"]
        try:
            count = conn.execute(f"SELECT COUNT(*) as n FROM {name}").fetchone()["n"]
        except Exception:
            count = "-"
        table.add_row(name, str(count))
    console.print(table)


@app.command("config")
def show_config() -> None:
    """Print the effective (non-secret) configuration."""
    s = get_settings()
    table = Table(title="Settings")
    table.add_column("key")
    table.add_column("value")
    for key in [
        "llm_provider",
        "has_llm_credentials",
        "db_path",
        "raw_cache_dir",
        "rate_limit_rps",
        "max_concurrency",
        "enable_playwright",
        "seed_size",
        "reviews_per_product",
    ]:
        table.add_row(key, str(getattr(s, key)))
    console.print(table)


@scrape_app.command("census")
def scrape_census(
    stats: bool = typer.Option(True, help="Also fetch Bazaarvoice bulk review statistics."),
    sitemap_check: bool = typer.Option(True, help="Cross-check coverage against the sitemap."),
) -> None:
    """Enumerate the entire catalog via GraphQL + Bazaarvoice statistics."""
    from petbarn_intel.scraping.discovery.census import run_census

    init_db(get_connection())

    async def _run():
        return await run_census(fetch_statistics=stats, cross_check_sitemap=sitemap_check)

    result = asyncio.run(_run())
    console.print(f"[green]Census complete[/green]: {result}")


@scrape_app.command("seed")
def scrape_seed(
    n: int = typer.Option(None, help="Target seed size (defaults to SEED_SIZE)."),
    dry_run: bool = typer.Option(False, help="Show the selection without marking tiers."),
) -> None:
    """Stratified selection of the seed set that gets a full deep scrape."""
    from collections import Counter

    from petbarn_intel.scraping.discovery.seed_selection import mark_as_seed, select_seed_products

    init_db(get_connection())
    selections = select_seed_products(target_size=n)
    by_pet_type = Counter(s.pet_type for s in selections)
    by_bucket = Counter(s.bucket for s in selections)

    table = Table(title=f"Seed selection ({len(selections)} products)")
    table.add_column("pet_type")
    table.add_column("count", justify="right")
    for pt, count in by_pet_type.most_common():
        table.add_row(pt, str(count))
    console.print(table)
    console.print(f"By bucket: {dict(by_bucket)}")

    if not dry_run:
        marked = mark_as_seed(selections)
        console.print(f"[green]Marked {marked} products as tier='seed'.[/green]")
    else:
        console.print("[yellow]Dry run -- no tiers changed.[/yellow]")


@scrape_app.command("deep")
def scrape_deep(
    limit: int = typer.Option(None, help="Only deep-scrape the first N seed products."),
    reviews_per_product: int = typer.Option(
        None, help="Review sample budget per product (defaults to REVIEWS_PER_PRODUCT)."
    ),
) -> None:
    """Deep scrape every tier='seed' product: GraphQL detail + JSON-LD
    cross-check, vendor AI summary, stratified review sample, Q&A."""
    from petbarn_intel.scraping.deep_scrape import run_deep_scrape

    init_db(get_connection())

    async def _run():
        return await run_deep_scrape(limit=limit, reviews_per_product=reviews_per_product)

    result = asyncio.run(_run())
    console.print(f"[green]Deep scrape complete[/green]: {result}")


@report_app.command("coverage")
def report_coverage(run_id: str = typer.Option(None, help="Restrict to one scrape run.")) -> None:
    """Per-field extraction completeness (plan section 1.7's detection
    layer): success rate and fallback-usage count per field, worst first."""
    from petbarn_intel.store import ops_repo

    init_db(get_connection())
    rows = ops_repo.field_completeness(run_id=run_id)
    table = Table(title="Field completeness" + (f" (run {run_id})" if run_id else " (all runs)"))
    table.add_column("field")
    table.add_column("completeness", justify="right")
    table.add_column("fallback_count", justify="right")
    table.add_column("attempts", justify="right")
    for r in rows:
        pct = f"{100 * r['completeness']:.1f}%"
        style = "red" if r["completeness"] < 0.7 else ("yellow" if r["completeness"] < 0.9 else "green")
        table.add_row(r["field_name"], f"[{style}]{pct}[/{style}]", str(r["fallback_count"]), str(r["attempts"]))
    console.print(table)


@report_app.command("incidents")
def report_incidents(all_: bool = typer.Option(False, "--all", help="Include resolved incidents.")) -> None:
    """Open (or all) data-quality incidents: missing fields, source
    disagreements, endpoint failures."""
    from petbarn_intel.store import ops_repo

    conn = get_connection()
    init_db(conn)
    rows = (
        conn.execute("SELECT * FROM incidents ORDER BY opened_at DESC LIMIT 200").fetchall()
        if all_
        else ops_repo.open_incidents(conn=conn)
    )
    table = Table(title=f"{'All' if all_ else 'Open'} incidents ({len(rows)})")
    table.add_column("opened_at")
    table.add_column("kind")
    table.add_column("severity")
    table.add_column("product_id")
    table.add_column("field")
    table.add_column("detail")
    for r in rows[:100]:
        sev_style = {"critical": "red", "warning": "yellow", "info": "dim"}.get(r["severity"], "")
        table.add_row(
            str(r["opened_at"])[:19], r["kind"], f"[{sev_style}]{r['severity']}[/{sev_style}]",
            r["product_id"] or "-", r["field_name"] or "-", (r["detail"] or "")[:60],
        )
    console.print(table)
    if len(rows) > 100:
        console.print(f"[dim]... and {len(rows) - 100} more[/dim]")


@report_app.command("cost")
def report_cost(limit: int = typer.Option(20, help="Number of recent turns to show.")) -> None:
    """Per-turn agent cost/latency/error summary from the trace store
    (plan section 1.8) -- one row per (session, turn)."""
    from petbarn_intel.store import ops_repo

    init_db(get_connection())
    rows = ops_repo.turn_cost_summary(limit=limit)
    table = Table(title=f"Recent agent turns (last {limit})")
    table.add_column("started_at")
    table.add_column("session")
    table.add_column("steps", justify="right")
    table.add_column("latency_ms", justify="right")
    table.add_column("cost_usd", justify="right")
    table.add_column("errors", justify="right")
    for r in rows:
        err_style = "red" if r["errors"] else ""
        table.add_row(
            str(r["started_at"])[:19], r["session_id"][:16], str(r["steps"]),
            f"{r['latency_ms']:.0f}" if r["latency_ms"] else "-",
            f"${r['cost_usd']:.4f}" if r["cost_usd"] else "$0.0000",
            f"[{err_style}]{r['errors']}[/{err_style}]" if err_style else str(r["errors"]),
        )
    console.print(table)


def run() -> None:
    app()


if __name__ == "__main__":
    run()
