
# Initialize the environment and make a directory

mkdir governed-db-agent && cd governed-db-agent
python3 -m venv venv && source venv/bin/activate
pip install langgraph langchain-anthropic langfuse
git init

