import asyncio
import os
import logging
from vendor.graphiti.mcp_server.src.graphiti_mcp_server import search_memory

# Disable verbose logging
logging.getLogger("graphiti_mcp_server").setLevel(logging.INFO)

async def main():
    print("--- Running Direct Search Verification ---")
    
    # This calls the same function the MCP tool uses
    result = await search_memory(
        query="What are the core entities in restaurant-backend?", 
        repo_id="restaurant-backend",
        max_facts=5,
        max_profiles=5
    )
    
    # Access as dictionary
    print(f"\nMessage: {result.get('message', 'No message')}")
    
    facts = result.get('neptune_facts', [])
    if facts:
        print(f"\n[Neo4j Facts found: {len(facts)}]")
        for f in facts[:3]:
            # Each fact in the response is also a dict
            print(f" - {f.get('fact')}")
            
    profiles = result.get('aurora_profiles', [])
    if profiles:
        print(f"\n[Semantic Profiles found: {len(profiles)}]")
        for p in profiles[:3]:
            print(f" - {p.get('value_text')}")

if __name__ == "__main__":
    asyncio.run(main())
