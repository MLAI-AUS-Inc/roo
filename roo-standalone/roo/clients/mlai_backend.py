"""
MLAI Backend Client

HTTP client for communicating with the mlai-backend service.
"""
from typing import Optional, Dict, Any

import httpx

from ..config import get_settings


class MLAIBackendClient:
    """Client for mlai-backend API."""
    
    def __init__(self):
        settings = get_settings()
        self.base_url = settings.MLAI_BACKEND_URL
        self.api_key = settings.MLAI_API_KEY
    
    @property
    def headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
            "Content-Type": "application/json"
        }
    
    async def save_article_generation(
        self,
        slack_user_id: str,
        job_id: str,
        domain: str,
        result: Optional[dict] = None
    ) -> dict:
        """
        Save article generation record to mlai-backend.
        
        Args:
            slack_user_id: Slack user who triggered generation
            job_id: Content Factory job ID
            domain: Target domain
            result: Optional generation result data
        
        Returns:
            Created record data
        """
        if not self.base_url:
            print("⚠️  MLAI_BACKEND_URL not configured, skipping save")
            return {}
        
        payload = {
            "slack_user_id": slack_user_id,
            "job_id": job_id,
            "domain": domain,
        }
        
        if result:
            payload.update({
                "topic": result.get("topic"),
                "title": result.get("title"),
                "slug": result.get("slug"),
                "meta_title": result.get("meta_title"),
                "meta_description": result.get("meta_description"),
                "keywords": result.get("keywords", []),
                "status": "completed"
            })
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/roo/article-generations/",
                json=payload,
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_user_by_slack_id(self, slack_id: str) -> Optional[Dict[str, Any]]:
        """
        Get user information by Slack ID.
        
        Args:
            slack_id: Slack user ID
        
        Returns:
            User data or None if not found
        """
        if not self.base_url:
            return None
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/api/roo/users/slack/{slack_id}/",
                    headers=self.headers,
                    timeout=10.0
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.json()
            except Exception as e:
                print(f"❌ User lookup failed: {e}")
                return None
    
    async def create_user(
        self,
        slack_id: str,
        name: str,
        email: Optional[str] = None
    ) -> dict:
        """Create a new user in mlai-backend."""
        if not self.base_url:
            return {}
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/roo/users/",
                json={
                    "slack_id": slack_id,
                    "name": name,
                    "email": email
                },
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    async def trigger_article_generation(
        self,
        slack_user_id: str,
        domain: str,
        topic: str,
        target_keyword: str,
        context: Optional[str] = None
    ) -> dict:
        """
        Trigger article generation via mlai-backend.
        
        Args:
            slack_user_id: Slack ID of requesting user
            domain: Target domain
            topic: Article topic
            target_keyword: Main keyword
            context: Optional conversation context
            
        Returns:
            Dict containing job_id and status
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
            
        payload = {
            "slack_user_id": slack_user_id,
            "domain": domain,
            "topic": topic,
            "target_keyword": target_keyword,
            "context": context
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/v1/content/generate",
                json=payload,
                headers=self.headers,
                timeout=30.0
            )
            response.raise_for_status()
            return response.json()

    async def discover_opportunities(
        self,
        domain: str,
        competitors: list[str],
        seed_keywords: Optional[list[str]] = None
    ) -> list[dict]:
        """
        Discover content opportunities via mlai-backend.
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
            
        payload = {
            "domain": domain,
            "competitors": competitors,
            "seed_keywords": seed_keywords
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/v1/content/discover",
                    json=payload,
                    headers=self.headers,
                    timeout=60.0
                )
                response.raise_for_status()
                data = response.json()
                return data.get("opportunities", [])
            except Exception as e:
                print(f"Discovery failed: {e}")
                raise

    async def check_generation_status(self, job_id: str) -> dict:
        """Check status of a generation job."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
            
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/api/v1/content/jobs/{job_id}",
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()

    async def publish_article(self, job_id: str) -> dict:
        """Request publication of a completed article."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
            
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/v1/content/publish/{job_id}",
                headers=self.headers,
                timeout=60.0
            )
            response.raise_for_status()
            return response.json()
