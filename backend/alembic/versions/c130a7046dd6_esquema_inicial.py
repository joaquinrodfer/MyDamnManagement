"""esquema inicial

Revision ID: c130a7046dd6
Revises:
Create Date: 2026-08-07 16:36:45.909753

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c130a7046dd6'
down_revision = None
branch_labels = None
depends_on = None

# Nota: `pages` y `databases` se referencian mutuamente (pages.database_id
# -> databases.id, databases.page_id -> pages.id). Autogenerate avisó con
# un SAWarning y no pudo resolver el ciclo por sí solo: se crea `databases`
# sin la FK a pages, se crea `pages`, y luego se añade esa FK aparte con
# create_foreign_key. Igual con `workspaces`, que debe existir antes que
# `pages` porque pages.workspace_id la referencia.


def upgrade() -> None:
    op.create_table('workspaces',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('databases',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('page_id', sa.UUID(), nullable=False),
        sa.Column('schema_def', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('page_id'),
    )

    op.create_table('pages',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('workspace_id', sa.UUID(), nullable=False),
        sa.Column('parent_id', sa.UUID(), nullable=True),
        sa.Column('database_id', sa.UUID(), nullable=True),
        sa.Column('type', sa.Enum('note', 'database', 'database_row', name='pagetype'), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('icon', sa.String(), nullable=True),
        sa.Column('properties', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['database_id'], ['databases.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['parent_id'], ['pages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_foreign_key(
        'fk_databases_page_id_pages', 'databases', 'pages', ['page_id'], ['id'], ondelete='CASCADE'
    )

    op.create_table('tags',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )

    op.create_table('attachments',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('page_id', sa.UUID(), nullable=False),
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('path', sa.String(), nullable=False),
        sa.Column('mime', sa.String(), nullable=True),
        sa.Column('size', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('links',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('source_page_id', sa.UUID(), nullable=False),
        sa.Column('target_page_id', sa.UUID(), nullable=False),
        sa.Column('kind', sa.Enum('wikilink', 'relation', name='linkkind'), nullable=False),
        sa.ForeignKeyConstraint(['source_page_id'], ['pages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_page_id'], ['pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table('page_content',
        sa.Column('page_id', sa.UUID(), nullable=False),
        sa.Column('body_markdown', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('page_id'),
    )

    op.create_table('page_tags',
        sa.Column('page_id', sa.UUID(), nullable=False),
        sa.Column('tag_id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(['page_id'], ['pages.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('page_id', 'tag_id'),
    )

    op.create_table('views',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('database_id', sa.UUID(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('type', sa.Enum('table', 'board', 'calendar', 'list', name='viewtype'), nullable=False),
        sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(['database_id'], ['databases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('views')
    op.drop_table('page_tags')
    op.drop_table('page_content')
    op.drop_table('links')
    op.drop_table('attachments')
    op.drop_table('tags')
    op.drop_constraint('fk_databases_page_id_pages', 'databases', type_='foreignkey')
    op.drop_table('pages')
    op.drop_table('databases')
    op.drop_table('workspaces')
