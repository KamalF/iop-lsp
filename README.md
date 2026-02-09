# IOP LSP Server

Language Server Protocol implementation for `.iop` files, providing:

- **Go-to-definition** — jump from a type reference to its definition
- **Hover documentation** — show doc comments and type info on hover

## Installation

### With uv (recommended)

```bash
cd tools/iop-lsp
uv sync    # Creates .venv and installs all dependencies
```

The `pyproject.toml` references `tree-sitter-iop` as a local path dependency
(`../../../tree-sitter-iop`). Adjust if your checkout is elsewhere.

### With pip

```bash
pip install tree-sitter pygls
pip install ~/dev/github/tree-sitter-iop
```

## Usage

```bash
# With uv
cd tools/iop-lsp && uv run python -m iop_lsp --stdio

# With pip (if packages installed globally/in active venv)
python -m iop_lsp --stdio

# With logging
uv run python -m iop_lsp --stdio --log-file /tmp/iop-lsp.log -v
```

## Editor Integration

The LSP server uses standard LSP over stdio and works with any editor.

### Helix

Add to `~/.config/helix/languages.toml`:

```toml
[language-server.iop-lsp]
command = "uv"
args = ["run", "--project", "/path/to/lib-common/tools/iop-lsp",
        "python", "-m", "iop_lsp", "--stdio"]

[[language]]
name = "iop"
scope = "source.iop"
file-types = ["iop"]
comment-token = "//"
block-comment-tokens = { start = "/*", end = "*/" }
indent = { tab-width = 4, unit = "    " }
language-servers = ["iop-lsp"]
```

Then in Helix:
- `gd` — Go to definition (on a type reference)
- `K` — Show hover documentation

### Neovim

With [nvim-lspconfig](https://github.com/neovim/nvim-lspconfig), add to your
Neovim configuration:

```lua
vim.api.nvim_create_autocmd('FileType', {
  pattern = 'iop',
  callback = function()
    vim.lsp.start({
      name = 'iop-lsp',
      cmd = { 'uv', 'run', '--project', '/path/to/lib-common/tools/iop-lsp',
              'python', '-m', 'iop_lsp', '--stdio' },
      root_dir = vim.fs.root(0, { '.git' }),
    })
  end,
})

-- Register .iop file type
vim.filetype.add({
  extension = {
    iop = 'iop',
  },
})
```

Then in Neovim:
- `gd` — Go to definition
- `K` — Show hover documentation

## Testing

```bash
cd tools/iop-lsp
uv run python -m pytest tests/ -v
```

## Architecture

- `iop_lsp/server.py` — LSP server, request handlers
- `iop_lsp/indexer.py` — Parse .iop files with tree-sitter, build symbol table
- `iop_lsp/symbols.py` — Symbol data structures
- `iop_lsp/doc_comments.py` — Extract doc comments from AST
