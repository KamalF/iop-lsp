# IOP LSP Server

Language Server Protocol implementation for `.iop` files, providing:

- **Go-to-definition** — jump from a type reference to its definition
- **Hover documentation** — show doc comments and type info on hover

## Prerequisites

- Python ≥ 3.9
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- [tree-sitter-iop](https://github.com/ilan-schemoul/tree-sitter-iop)
  checked out locally

## Installation

```bash
cd /path/to/iop-lsp
uv sync    # Creates .venv and installs all dependencies
```

The `pyproject.toml` references `tree-sitter-iop` as a local path dependency
(`../tree-sitter-iop`).  If your checkout is elsewhere, edit
`[tool.uv.sources]` in `pyproject.toml` to point to it, then run
`uv sync` again.

## Usage

```bash
# Start the LSP server (editors do this automatically)
uv run --project /path/to/iop-lsp python -m iop_lsp --stdio

# With logging (useful for debugging)
uv run --project /path/to/iop-lsp python -m iop_lsp --stdio \
    --log-file /tmp/iop-lsp.log -v
```

## Editor Integration

The LSP server uses standard LSP over stdio and works with any editor.
On startup, it recursively indexes all `.iop` files under the workspace
root to build a symbol table used for go-to-definition and hover.

### Neovim

No plugins required — Neovim ≥ 0.11 has built-in LSP support.

**Quick install:** run `./install-nvim-iop-lsp.sh` to automatically
configure your `~/.vimrc` (use `--uninstall` to revert).

**Manual setup:** add the following to your Neovim configuration
(e.g., `~/.config/nvim/init.lua`):

```lua
-- Teach Neovim about the .iop file extension.
vim.filetype.add({
    extension = { iop = 'iop' },
})

-- Start iop-lsp for .iop.
-- Adjust the --project path to where you cloned iop-lsp.
vim.api.nvim_create_autocmd('FileType', {
    pattern = { 'iop' },
    callback = function()
        vim.lsp.start({
            name = 'iop-lsp',
            cmd = {
                'uv', 'run',
                '--project', '/path/to/iop-lsp',
                'python', '-m', 'iop_lsp', '--stdio',
            },
            -- Walk up to .git so the LSP indexes all .iop files
            -- in the project.  Cross-file go-to-definition only
            -- works within this root.
            root_dir = vim.fs.root(0, { '.git' }),
        })
    end,
})
```

**How it works:**

- `root_dir` determines which `.iop` files are indexed.  The LSP
  recursively scans this directory on startup.  Set it to the root of
  your project (e.g., lib-common) so that all type definitions are
  available for cross-file navigation.
- `vim.lsp.start()` reuses the same LSP server for all buffers that
  share the same `root_dir`, so opening multiple `.iop` files in the same
  project is efficient.

**Keybindings:**  Neovim ≥ 0.11 maps `K` to hover automatically.
Go-to-definition must be mapped manually (e.g., via an `LspAttach`
autocmd or your editor distribution's LSP keybindings).  The built-in
`gd` is Vim's "go to local declaration" and does **not** call the LSP.

Example keybindings:

```lua
vim.api.nvim_create_autocmd('LspAttach', {
    callback = function(args)
        local opts = { buffer = args.buf }
        vim.keymap.set('n', 'gd', vim.lsp.buf.definition, opts)
        vim.keymap.set('n', '<leader>e', vim.diagnostic.open_float, opts)
        vim.keymap.set('n', '[d', vim.diagnostic.goto_prev, opts)
        vim.keymap.set('n', ']d', vim.diagnostic.goto_next, opts)
    end,
})
```

#### Tree-sitter highlighting (optional)

If you have
[tree-sitter-iop](https://github.com/ilan-schemoul/tree-sitter-iop)
built locally with its `iop.so` parser, you can enable syntax
highlighting for `.iop` files without any plugin:

```lua
-- Adjust the path to your tree-sitter-iop checkout.
local ts_iop = '/path/to/tree-sitter-iop'

pcall(function()
    vim.treesitter.language.add('iop', { path = ts_iop .. '/iop.so' })
    vim.opt.runtimepath:prepend(ts_iop)  -- finds queries/iop/highlights.scm
end)

vim.api.nvim_create_autocmd('FileType', {
    pattern = 'iop',
    callback = function() vim.treesitter.start() end,
})
```

### Helix

Add to `~/.config/helix/languages.toml`:

```toml
[language-server.iop-lsp]
command = "uv"
args = ["run", "--project", "/path/to/iop-lsp",
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

## Testing

```bash
uv run python -m pytest tests/ -v
```

## Architecture

- `iop_lsp/server.py` — LSP server, request handlers
- `iop_lsp/indexer.py` — Parse .iop files with tree-sitter, build symbol table
- `iop_lsp/symbols.py` — Symbol data structures
- `iop_lsp/doc_comments.py` — Extract doc comments from AST
- `iop_lsp/schema.py` — IOP type resolution for YAML support
- `iop_lsp/yaml_support.py` — YAML parsing and cursor-to-type mapping
