import os
import uuid
import hashlib
import psycopg
from psycopg.conninfo import conninfo_to_dict

HOST = "dpg-dbi0vsei0phs73ch9pdg-a"
DB = "elcohe_juris_164_db"
USER = "elcohe_juris_nuevo"

def ejecutar():
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL ausente")

    datos = conninfo_to_dict(url)
    if (datos.get("host"), datos.get("dbname"), datos.get("user")) != (HOST, DB, USER):
        raise RuntimeError("Base de datos no autorizada")

    identificador = "PRUEBA-" + uuid.uuid4().hex
    huella = hashlib.sha256(identificador.encode()).hexdigest()

    with psycopg.connect(url, autocommit=False) as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user")
                if cur.fetchone() != (DB, USER):
                    raise RuntimeError("Identidad PostgreSQL incorrecta")

                cur.execute("""
                    SELECT to_regclass('public.elcohe_decisions'),
                           to_regclass('public.elcohe_holding_approvals')
                """)
                if any(x is None for x in cur.fetchone()):
                    raise RuntimeError("Tablas jurisprudenciales inexistentes")

                cur.execute("""
                    INSERT INTO public.elcohe_decisions
                    (court, decision_number, source_url, sha256,
                     legal_issue, verification_status,
                     review_required, ratio_decidendi_verified)
                    VALUES ('TC', %s, %s, %s, %s,
                            'VERIFIED_SOURCE', TRUE, FALSE)
                    RETURNING id
                """, (
                    identificador,
                    "https://example.invalid/prueba",
                    huella,
                    "PRUEBA TÉCNICA - NO ES SENTENCIA"
                ))
                registro = cur.fetchone()[0]

                cur.execute("""
                    SELECT decision_number, sha256,
                           review_required, ratio_decidendi_verified
                    FROM public.elcohe_decisions WHERE id=%s
                """, (registro,))

                if cur.fetchone() != (identificador, huella, True, False):
                    raise RuntimeError("Recuperación incorrecta")

                cur.execute("""
                    SELECT COUNT(*) FROM public.elcohe_holding_approvals
                    WHERE decision_id=%s
                """, (registro,))

                if cur.fetchone()[0] != 0:
                    raise RuntimeError("Aprobación jurídica inesperada")

            conn.rollback()

            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM public.elcohe_decisions
                    WHERE decision_number=%s
                """, (identificador,))

                if cur.fetchone()[0] != 0:
                    raise RuntimeError("Reversión no comprobada")

            print("ELCOHE_REGISTRO_165: PRUEBA SATISFACTORIA")
            print("Inserción y recuperación: CORRECTAS")
            print("Aprobaciones automáticas: NINGUNA")
            print("Reversión: VERIFICADA")
        finally:
            conn.rollback()

if __name__ == "__main__":
    ejecutar()
