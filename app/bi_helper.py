"""
Helper compartido de acceso a la BI (Postgres health_bi) para los módulos Alma.

Centraliza el patrón de pool + query + degradación elegante que antes cada router
reimplementaba. Si la BI no está disponible, bi_query devuelve [] (los módulos
muestran vacío con source_status, nunca 500).
"""
import logging

log = logging.getLogger("bi_helper")


def bi_query(sql: str, params: tuple = ()) -> tuple[list[dict], str]:
    """Ejecuta una query contra la BI. Devuelve (filas_dict, status).

    status: "ok" | "bi_unavailable". Nunca lanza por caída de BI.
    """
    try:
        from main import _bi_pool
        pool = _bi_pool()
        conn = None
        try:
            conn = pool.getconn()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description is None:
                    return [], "ok"
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()], "ok"
        finally:
            if conn is not None:
                pool.putconn(conn)
    except Exception as e:
        log.warning("BI no disponible: %s", e)
        return [], "bi_unavailable"
