import asyncio
import os
from neo4j import AsyncGraphDatabase

async def main():
    uri = "bolt://localhost:7687"
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "contextgraph")
    print(f"Connecting to {uri} with user '{user}' and password '{password}'")
    
    auth_token = (user, password)
    driver = AsyncGraphDatabase.driver(uri, auth=auth_token)
    
    try:
        await driver.verify_connectivity()
        print("Successfully connected!")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        await driver.close()

if __name__ == "__main__":
    asyncio.run(main())
