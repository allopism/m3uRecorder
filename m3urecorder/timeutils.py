"""
Fechas y horas. Internamente todo se calcula en UTC (inmune a los cambios de hora);
la hora local solo se usa para mostrar y para interpretar lo que escribe el usuario.

La zona horaria y sus cambios de hora los resuelve zoneinfo con la base de datos
tzdata, así que funciona para cualquier zona, no solo la europea.
"""
from datetime import UTC, datetime, timezone
from zoneinfo import ZoneInfo

_zone = ZoneInfo("Europe/Madrid")


def set_timezone(name: str) -> None:
    global _zone
    _zone = ZoneInfo(name)


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_local(dt: datetime) -> datetime:
    """Convierte un datetime (naive = UTC) a la hora local con su offset (+01:00 / +02:00)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_zone)


def now_local() -> datetime:
    return to_local(now_utc())


def parse_local(dt_str: str, fold: int = 0) -> datetime:
    """
    Interpreta un ISO-8601. Si trae offset se respeta; si no, se toma como hora local.

    `fold` resuelve la hora repetida del cambio de octubre (02:00-03:00 ocurre dos
    veces): 0 = la primera (horario de verano), 1 = la segunda (horario de invierno).
    Se devuelve con un offset fijo para que al guardarlo en JSON no se pierda cuál era.
    """
    dt = datetime.fromisoformat(dt_str.strip())
    if dt.tzinfo is not None:
        return to_local(dt)
    aware = dt.replace(tzinfo=_zone, fold=1 if int(fold) == 1 else 0)
    return dt.replace(tzinfo=timezone(aware.utcoffset(), aware.tzname()))


def to_utc(dt_or_str) -> datetime:
    dt = parse_local(dt_or_str) if isinstance(dt_or_str, str) else dt_or_str
    if dt.tzinfo is None:
        dt = parse_local(dt.isoformat())
    return dt.astimezone(UTC)


def seconds_between(start, end) -> int:
    """Segundos reales entre dos instantes (correcto aunque haya un cambio de hora en medio)."""
    return int(round((to_utc(end) - to_utc(start)).total_seconds()))
