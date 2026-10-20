brew install uv
#curl -LsSf https://astral.sh/uv/install.sh | sh

# Create a new directory for our project
uv init r1
cd r1

# Create virtual environment and activate it
uv venv
source .venv/bin/activate

# Install dependencies
uv add "mcp[cli]" httpx fastmcp langchain-mcp-adapters langgraph langchain "langchain[openai]" html2text bs4 langchain-community langchain-core pandas selenium webdriver-manager pypdf 

# Install Markdownify-MCP
git clone https://github.com/zcaceres/markdownify-mcp.git
cd markdownify-mcp
npm install -g pnpm@latest-10   # install pnpm if not already installed

pnpm install
pnpm run build      # create the distributable version that can be called at runtime
#pnpm start         # start the mcp server for markdownify
#pnpm run dev       # or run in dev mode
#uv add "

# create environment variables including the API key for OpenAI
#touch dotenv.env


# https://stackoverflow.com/questions/69097224/gobject-2-0-0-not-able-to-load-on-macbook
# sudo ln -s /opt/homebrew/lib /usr/local/lib
# export LDFLAGS=-L/opt/homebrew/lib
# export DYLD_LIBRARY_PATH=/opt/homebrew/lib