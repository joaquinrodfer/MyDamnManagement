"""`GET /people`: las personas de la app, para asignar responsables.

Una celda de tipo `person` guarda el **`id`** de un usuario de la tabla
`users`, y el selector de la celda necesita la lista con la que elegir: nombre
visible, @usuario y color. No es `GET /auth/users`, que es del panel de
administración (solo contesta a un admin y lleva `is_admin`): repartir un
responsable es cosa de cualquiera que use la app, y aquí no hay nada de
credenciales. Las dos rutas leen de la misma tabla y devuelven `as_dict()` sin
hash ni salt.
"""

from fastapi import APIRouter, Depends

from app import auth
from app.dependencies import get_current_user, get_repo
from app.repository import Repository

router = APIRouter(prefix="/people", tags=["people"])


@router.get("")
def list_people(
    repo: Repository = Depends(get_repo),
    user: auth.User = Depends(get_current_user),
):
    """Las personas que se pueden asignar en una celda `person`.

    Por nombre visible, que es como se leen en la tabla y en el selector.
    """
    return repo.all_users()
