"""
Content Factory Client

HTTP client for the Content Factory article generation service.
This module is the implementation backing the content_factory skill.
"""
import asyncio
from typing import Optional, Callable

import httpx


class ContentFactoryClient:
    """Client for Content Factory API."""
    
    def __init__(self, base_url: str, api_key: str):
        """
        Initialize the Content Factory client.
        
        Args:
            base_url: Base URL of the Content Factory API (e.g., http://1.2.3.4:8000)
            api_key: API key for authentication
        """
        self.base_url = base_url
        self.api_key = api_key
        
        if not self.base_url:
            raise ValueError("CONTENT_FACTORY_URL not configured")
    
    @property
    def headers(self) -> dict:
        return {
            "X-API-Key": self.api_key or "",
            "Content-Type": "application/json"
        }
    
    async def generate_article(
        self,
        domain: str,
        topic: str,
        target_keyword: str,
        context: Optional[str] = None,
        slack_user_id: Optional[str] = None
    ) -> str:
        """
        Direct Mode: Generates an article for a specific topic/keyword.
        
        Args:
            domain: User's domain (e.g., "mlai.au")
            topic: Specific topic title (e.g., "AI Hackathons")
            target_keyword: Main keyword to target (e.g., "hackathon melbourne")
            context: Optional conversation context/thread history
            slack_user_id: Optional Slack user ID to associate with the job
            
        Returns:
            job_id: The generation job ID
        """
        payload = {
            "domain": domain,
            "topic": topic,
            "target_keyword": target_keyword,
        }
        if context:
            payload["context"] = context
        if slack_user_id:
            payload["slack_user_id"] = slack_user_id
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/pipeline/generate",
                json=payload,
                headers=self.headers,
                timeout=30.0
            )
            response.raise_for_status()
            
            data = response.json()
            job_id = data.get("job_id")
            
            if not job_id:
                raise Exception("No job_id returned from generate endpoint")
            
            print(f"📝 Content generation started: {job_id}")
            return job_id

    async def discover_opportunities(
        self,
        domain: str,
        competitors: list[str],
        seed_keywords: Optional[list[str]] = None
    ) -> list[dict]:
        """
        Discovery Mode: Analyzes competitors to find content opportunities.
        
        Args:
            domain: User's domain
            competitors: List of competitor domains to analyze
            seed_keywords: Optional hints for discovery
            
        Returns:
            List of opportunity dicts (keyword, volume, difficulty, etc)
        """
        payload = {
            "domain": domain,
            "competitors": competitors
        }
        if seed_keywords:
            payload["seed_keywords"] = seed_keywords
            
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.base_url}/api/pipeline/discover",
                    json=payload,
                    headers=self.headers,
                    timeout=60.0
                )
                response.raise_for_status()
                
                data = response.json()
                if data.get("status") != "success":
                    raise Exception(f"Discovery failed: {data.get('error')}")
                    
                return data.get("opportunities", [])
                
            except httpx.RequestError as e:
                print(f"Content Factory Discover API Error: {e}")
                raise Exception(f"Failed to discover opportunities: {e}")
    
    async def get_job_status(self, job_id: str) -> dict:
        """Get current job status."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/api/pipeline/status/{job_id}",
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json()
    
    async def get_job_result(self, job_id: str) -> dict:
        """Get completed job result."""
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/api/pipeline/result/{job_id}",
                headers=self.headers,
                timeout=10.0
            )
            response.raise_for_status()
            return response.json().get("result", {})
    
    async def poll_and_wait(
        self,
        job_id: str,
        on_progress: Optional[Callable[[dict], None]] = None,
        poll_interval: float = 5.0
    ) -> dict:
        """
        Poll job until completion.
        
        Args:
            job_id: Job ID to poll
            on_progress: Optional callback for progress updates
            poll_interval: Seconds between polls
        
        Returns:
            Final job result
        """
        while True:
            status_data = await self.get_job_status(job_id)
            state = status_data["status"]
            progress = status_data.get("progress", 0)
            step = status_data.get("current_step", "unknown")
            
            print(f"   Status: {state} ({progress}%) - {step}")
            
            if on_progress:
                try:
                    on_progress(status_data)
                except Exception as e:
                    print(f"   Progress callback error: {e}")
            
            if state == "completed":
                break
            elif state == "failed":
                raise Exception(f"Job failed: {status_data.get('error', 'Unknown')}")
            
            await asyncio.sleep(poll_interval)
        
        return await self.get_job_result(job_id)
    
    async def publish_article(self, job_id: str, slack_user_id: Optional[str] = None) -> dict:
        """
        Publish a completed article via the publish endpoint.
        
        Args:
            job_id: The job ID of the completed article
            
        Returns:
            Dict with preview info
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/pipeline/publish/{job_id}",
                json={"slack_user_id": slack_user_id} if slack_user_id else {},
                headers=self.headers,
                timeout=60.0
            )
            response.raise_for_status()
            
            data = response.json()
            
            if data.get("status") == "success":
                publish_data = data.get("data", {})
                return {
                    "success": True,
                    "preview_url": publish_data.get("preview_url"),
                    "primary_action_url": publish_data.get("primary_action_url"),
                    "primary_action_label": publish_data.get("primary_action_label"),
                    "pr_url": publish_data.get("pr_url"),
                    "pr_number": publish_data.get("pr_number"),
                    "branch_name": publish_data.get("branch_name"),
                    "branch_url": publish_data.get("branch_url"),
                    "file_path": publish_data.get("file_path"),
                    "message": publish_data.get("message", "Content published successfully")
                }
            else:
                raise Exception(f"Publish failed: {data.get('error')}")
