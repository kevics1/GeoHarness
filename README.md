# GeoHarness

Geography-oriented native agent harness built on OpenHarness.

## Overview

GeoHarness is a domain-specific agent system for spatial analysis and cartography, built on top of OpenHarness without modifying its source code.

- **Primary goal**: Spatial analysis
- **Secondary output**: Cartography as a product of analysis
- **Cognitive cascade**: L1 (geo-perception) → L2 (geo-comprehension) → L3 (geo-reasoning)

## Installation

```bash
pip install -e ".[dev]"
```

## Quick Start

```bash
geoh init      # Create config directory (~/.geoharness/)
geoh dry-run   # Verify runtime assembly
geoh run       # Launch TUI (Phase 7)
```

## Configuration

Edit `~/.geoharness/config.yaml` and `~/.geoharness/.env` to configure:
- LLM API (model, key, base URL)
- MCP servers (geo-mcp-server, qgis, postgres)
- Cartography (template, bridge rules)
- Data sources (PostGIS DSN, CRS)

## License

MIT
