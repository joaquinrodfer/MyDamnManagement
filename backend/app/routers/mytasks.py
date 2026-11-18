"""`GET /my-tasks`: la vista que cruza todas las bases, servida de una vez.

Una llamada, no cuarenta. El cálculo vive en `app.mytasks` (una consulta para
los schemas y una para las filas de las bases que tienen responsable); aquí
solo se le pasa el usuario de la sesión, que es lo único que decide qué es
«mío».
"""

from fastapi import APIRouter, Depends

from app import acl, auth, mytasks, schemas
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(tags=["my-tasks"])


@router.get("/my-tasks", response_model=schemas.MyTasksRead)
def get_my_tasks(
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Las filas de todas las bases cuyo responsable es quien pregunta.

    Sin sesión, el middleware contesta 401 como en el resto de la API: esto es
    «mis» tareas, y sin saber quién eres no hay nada que devolver.
    """
    return schemas.MyTasksRead(
        **mytasks.my_tasks(repo.conn, user.id, acl.visible_ids(repo.conn, user, repo.workspace_id))
    )
