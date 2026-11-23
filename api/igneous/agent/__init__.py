"""The research agent (#92): multi-hop briefings with citations.

`research.py` runs it: a hosted model reads the graph through the MCP server's own tools (`tools.py`), one turn at a
time within a budget, and keeps a ledger of what it read; `report.py` then writes the briefing from that ledger, every
statement citing records the agent read. POST /research, the MCP `research` tool and `igneous research` start it.
"""
