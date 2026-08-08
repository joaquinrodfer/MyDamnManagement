"""Plantillas preconfiguradas de CRM y Tareas (Fase 3).

Deliberadamente no son código nuevo del motor: son un `schema_def` y un
conjunto de `view` por defecto, pasados a las mismas operaciones de
creación que ya existían en la Fase 2 (crear database, crear view). Si
esto necesitara lógica propia, sería la señal de que el motor genérico
no era suficientemente genérico.
"""

TEMPLATES: dict[str, dict] = {
    "crm": {
        "title": "Contactos",
        "schema_def": [
            {"key": "empresa", "name": "Empresa", "type": "text"},
            {
                "key": "fase",
                "name": "Fase",
                "type": "select",
                "options": ["lead", "contactado", "propuesta", "ganado", "perdido"],
            },
            {"key": "valor", "name": "Valor", "type": "number"},
            {"key": "proximo_contacto", "name": "Próximo contacto", "type": "date"},
        ],
        "views": [
            {"name": "Pipeline", "type": "board", "config": {"group_by": "fase"}},
            {"name": "Tabla", "type": "table", "config": {}},
        ],
    },
    "tasks": {
        "title": "Tareas",
        "schema_def": [
            {
                "key": "estado",
                "name": "Estado",
                "type": "select",
                "options": ["pendiente", "en_progreso", "hecha"],
            },
            {
                "key": "prioridad",
                "name": "Prioridad",
                "type": "select",
                "options": ["baja", "media", "alta"],
            },
            {"key": "fecha_limite", "name": "Fecha límite", "type": "date"},
            {"key": "proyecto", "name": "Proyecto", "type": "text"},
        ],
        "views": [
            {"name": "Tablero", "type": "board", "config": {"group_by": "estado"}},
            {"name": "Tabla", "type": "table", "config": {}},
        ],
    },
}
