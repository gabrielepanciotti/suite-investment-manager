from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, text
from suite_backend_core import create_app

from . import db
from .errors import register_error_handlers
from .pagination import Page, decode_cursor, encode_cursor
from .routers.portfolio import router as portfolio_router
from .routers.positions import router as positions_router
from .schemas.positions import PositionOut


async def _db_ping() -> None:
    """Fail health checks when the node database is unavailable or unconfigured."""
    if db._session_factory is None:
        raise RuntimeError("DATABASE_URL is not configured")

    async with db._session_factory() as session:
        await session.execute(text("SELECT 1"))


app = create_app(db_ping=_db_ping)
register_error_handlers(app)

app.include_router(positions_router)
app.include_router(portfolio_router)

standard_router = APIRouter(prefix="/api", tags=["standard"])


@standard_router.get("/entries", response_model=Page[PositionOut])
async def list_entries(
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> Page[PositionOut]:
    """List archived positions with the standard Life cursor envelope."""
    try:
        offset = decode_cursor(cursor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"cursor": str(exc)}) from exc

    if db._session_factory is None:
        raise HTTPException(
            status_code=503, detail="database del nodo non configurato"
        )

    async with db._session_factory() as session:
        total = await session.scalar(select(func.count()).select_from(db.Position))
        rows = (
            await session.execute(
                select(db.Position)
                .order_by(db.Position.created_at.desc(), db.Position.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()

    count = int(total or 0)
    next_offset = offset + len(rows)
    return Page[PositionOut](
        items=[PositionOut.model_validate(row) for row in rows],
        total=count,
        next_cursor=encode_cursor(next_offset) if next_offset < count else None,
    )


app.include_router(standard_router)


@app.get("/overview")
async def overview() -> dict[str, object]:
    """Return the stable node identity and its currently mounted routes."""
    routes = sorted(app.openapi()["paths"])
    return {
        "name": "InvestmentManager",
        "slug": "investmentManager",
        "famiglia": "tracker",
        "routes": routes,
    }
