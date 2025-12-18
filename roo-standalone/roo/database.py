"""
Database Layer

PostgreSQL + pgvector for vector similarity search.
"""
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text

from .config import get_settings


class Database:
    """PostgreSQL database with pgvector support."""
    
    def __init__(self, database_url: str):
        import ssl
        import re
        
        # Ensure we're using the async driver
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif not database_url.startswith("postgresql+asyncpg://"):
            database_url = "postgresql+asyncpg://" + database_url.split("://", 1)[-1]
        
        # Handle SSL mode for asyncpg (doesn't support sslmode= param directly)
        # Convert sslmode=require to asyncpg's ssl=True via connect_args
        connect_args = {}
        if "sslmode=" in database_url:
            # Remove sslmode from URL
            database_url = re.sub(r'[\?&]sslmode=[^&]*', '', database_url)
            # Clean up any remaining & at start of query
            database_url = database_url.replace('?&', '?').rstrip('?')
            # Use SSL context for asyncpg
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            connect_args["ssl"] = ssl_context
        
        self.engine = create_async_engine(database_url, echo=False, connect_args=connect_args)
        self.async_session = async_sessionmaker(
            self.engine, 
            class_=AsyncSession,
            expire_on_commit=False
        )
        
        # Ensure tables exist (simple migration)
        import asyncio
        # We can't await in __init__, so we'll need to do this carefully or rely on app startup
        # For now, we'll expose an init method

    
    @asynccontextmanager
    async def session(self):
        """Get an async database session."""
        async with self.async_session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    
    async def vector_search(
        self,
        query: str,
        table: str = "user_expertise",
        limit: int = 5,
        threshold: float = 0.7
    ) -> List[Dict[str, Any]]:
        """
        Perform cosine similarity search using pgvector.
        
        Args:
            query: Text to search for (will be embedded)
            table: Table to search in
            limit: Maximum results
            threshold: Minimum similarity threshold
        
        Returns:
            List of matching records with similarity scores
        """
        from .llm import embed
        
        # Generate embedding for query
        query_embedding = await embed(query)
        
        async with self.session() as session:
            # Format embedding as PostgreSQL vector
            embedding_str = "[" + ",".join(map(str, query_embedding)) + "]"
            
            if table == "user_expertise":
                result = await session.execute(
                    text("""
                        SELECT 
                            u.id, u.first_name || ' ' || u.last_name as name, u.personas->>'slack_id' as slack_id,
                            e.topic, e.relationship,
                            1 - (e.embedding <=> :query_embedding::vector) as similarity
                        FROM core_user u
                        JOIN user_expertise e ON u.id = e.user_id
                        WHERE 1 - (e.embedding <=> :query_embedding::vector) > :threshold
                        ORDER BY similarity DESC
                        LIMIT :limit
                    """),
                    {
                        "query_embedding": embedding_str,
                        "threshold": threshold,
                        "limit": limit
                    }
                )
            elif table == "knowledge_documents":
                result = await session.execute(
                    text("""
                        SELECT 
                            id, title, content, source,
                            1 - (embedding <=> :query_embedding::vector) as similarity
                        FROM knowledge_documents
                        WHERE 1 - (embedding <=> :query_embedding::vector) > :threshold
                        ORDER BY similarity DESC
                        LIMIT :limit
                    """),
                    {
                        "query_embedding": embedding_str,
                        "threshold": threshold,
                        "limit": limit
                    }
                )
            else:
                raise ValueError(f"Unknown table: {table}")
            
            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]
    
    async def execute(self, query: str, params: dict = None) -> List[Dict[str, Any]]:
        """Execute a raw SQL query."""
        async with self.session() as session:
            result = await session.execute(text(query), params or {})
            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]
    
    async def get_user_by_slack_id(
        self,
        slack_id: str
    ) -> Optional[int]:
        """Get user ID by Slack ID (read-only)."""
        async with self.session() as session:
            # Query core_user using JSONB personas field
            result = await session.execute(
                text("SELECT id FROM core_user WHERE personas->>'slack_id' = :slack_id"),
                {"slack_id": slack_id}
            )
            return result.scalar_one_or_none()
    
    async def get_user_by_email(self, email: str) -> Optional[int]:
        """Get user ID by email (case-insensitive)."""
        async with self.session() as session:
            result = await session.execute(
                text("SELECT id FROM core_user WHERE lower(email) = lower(:email)"),
                {"email": email}
            )
            return result.scalar_one_or_none()

    async def link_user_slack_id(self, user_id: int, slack_id: str):
        """Link a Slack ID to an existing user by updating personas."""
        async with self.session() as session:
            # We use jsonb_set to update the slack_id within the personas JSONB column
            # If personas is null, we treat it as empty dict
            await session.execute(
                text("""
                    UPDATE core_user 
                    SET personas = jsonb_set(COALESCE(personas, '{}'), '{slack_id}', to_jsonb(:slack_id::text))
                    WHERE id = :user_id
                """),
                {
                    "user_id": user_id,
                    "slack_id": slack_id
                }
            )
    
    async def add_user_expertise(
        self,
        user_id: int,
        topic: str,
        relationship: str = "interested"
    ):
        """Add expertise for a user with embedding."""
        from .llm import embed
        
        # Generate embedding for the topic
        embedding = await embed(topic)
        embedding_str = "[" + ",".join(map(str, embedding)) + "]"
        
        async with self.session() as session:
            await session.execute(
                text("""
                    INSERT INTO user_expertise (user_id, topic, relationship, embedding)
                    VALUES (:user_id, :topic, :relationship, :embedding::vector)
                    ON CONFLICT DO NOTHING
                """),
                {
                    "user_id": user_id,
                    "topic": topic,
                    "relationship": relationship,
                    "embedding": embedding_str
                }
            )


    async def init_integrations_table(self):
        """Initialize the user_integrations table."""
        async with self.session() as session:
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS user_integrations (
                    slack_user_id TEXT PRIMARY KEY,
                    github_access_token TEXT,
                    github_user_name TEXT,
                    github_scopes TEXT[],
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    project_scanned BOOLEAN DEFAULT FALSE,
                    pending_intent TEXT
                )
            """))

    async def save_github_token(
        self,
        slack_user_id: str,
        token: str,
        user_name: str = None,
        scopes: List[str] = None
    ):
        """Save or update a GitHub access token."""
        async with self.session() as session:
            await session.execute(
                text("""
                    INSERT INTO user_integrations (slack_user_id, github_access_token, github_user_name, github_scopes, updated_at)
                    VALUES (:uid, :token, :name, :scopes, CURRENT_TIMESTAMP)
                    ON CONFLICT (slack_user_id) 
                    DO UPDATE SET 
                        github_access_token = EXCLUDED.github_access_token,
                        github_user_name = COALESCE(EXCLUDED.github_user_name, user_integrations.github_user_name),
                        github_scopes = COALESCE(EXCLUDED.github_scopes, user_integrations.github_scopes),
                        updated_at = CURRENT_TIMESTAMP
                """),
                {
                    "uid": slack_user_id,
                    "token": token,
                    "name": user_name,
                    "scopes": scopes
                }
            )

    async def get_github_token(self, slack_user_id: str) -> Optional[str]:
        """Retrieve GitHub access token for a user."""
        async with self.session() as session:
            result = await session.execute(
                text("SELECT github_access_token FROM user_integrations WHERE slack_user_id = :uid"),
                {"uid": slack_user_id}
            )
            return result.scalar_one_or_none()

    async def get_integration(self, slack_user_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve full integration record."""
        async with self.session() as session:
            result = await session.execute(
                text("SELECT * FROM user_integrations WHERE slack_user_id = :uid"),
                {"uid": slack_user_id}
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None

    async def save_pending_intent(self, slack_user_id: str, intent_data: str):
        """Save a pending action to resume after auth."""
        async with self.session() as session:
            await session.execute(
                text("""
                    INSERT INTO user_integrations (slack_user_id, pending_intent)
                    VALUES (:uid, :intent)
                    ON CONFLICT (slack_user_id)
                    DO UPDATE SET pending_intent = EXCLUDED.pending_intent
                """),
                {"uid": slack_user_id, "intent": intent_data}
            )

    async def clear_pending_intent(self, slack_user_id: str):
        """Clear the pending intent."""
        async with self.session() as session:
            await session.execute(
                text("UPDATE user_integrations SET pending_intent = NULL WHERE slack_user_id = :uid"),
                {"uid": slack_user_id}
            )

    async def mark_project_scanned(self, slack_user_id: str, scanned: bool = True):
        """Update the project_scanned status."""
        async with self.session() as session:
            await session.execute(
                text("UPDATE user_integrations SET project_scanned = :scanned WHERE slack_user_id = :uid"),
                {"uid": slack_user_id, "scanned": scanned}
            )
# Singleton database instance
_db: Optional[Database] = None


def get_db() -> Database:
    """Get or create the database singleton."""
    global _db
    if _db is None:
        settings = get_settings()
        _db = Database(settings.DATABASE_URL)
    return _db
