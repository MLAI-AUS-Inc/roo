"""
GitHub Integration Client

Handles interactions with GitHub repositories via the Content Factory.
"""
import httpx
from typing import Optional, Dict, Any

class GitHubIntegrationClient:
    """Client for GitHub Integration actions."""
    
    def __init__(self, content_factory_url: str, api_key: str):
        self.base_url = content_factory_url
        self.api_key = api_key
        
    async def scan_repo(
        self,
        repo_name: str,
        github_token: str,
        domain: str = None
    ) -> Dict[str, Any]:
        """
        Trigger a repository scan via the Content Factory.
        
        Args:
            repo_name: "owner/repo"
            github_token: The user's GitHub access token
            domain: Optional domain name
            
        Returns:
            JSON response from Content Factory
        """
        if not self.base_url:
            raise ValueError("CONTENT_FACTORY_URL not configured")
            
        payload = {
            "github_repo": repo_name,
            "github_token": github_token,
            "scaffold": True  # Default to scaffolding/analyzing
        }
        
        if domain:
            payload["domain"] = domain
            
        headers = {
            "X-API-Key": self.api_key or "",
            "Content-Type": "application/json"
        }
        
        print(f"🔍 Requesting scan for {repo_name}...")
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/api/pipeline/scan",
                json=payload,
                headers=headers,
                timeout=60.0
            )
            response.raise_for_status()
            return response.json()
