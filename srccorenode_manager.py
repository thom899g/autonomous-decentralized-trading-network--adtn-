"""
Autonomous Node Manager
Responsible for orchestrating AI trading nodes, managing lifecycle, and handling consensus.
Architectural Choice: Uses singleton pattern for node coordination to ensure single source of truth
across decentralized nodes via Firebase as coordination layer.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum
import firebase_admin
from firebase_admin import firestore, db
import structlog
from datetime import datetime, timedelta
import hashlib
import json

logger = structlog.get_logger(__name__)

class NodeStatus(Enum):
    """Node operational states with failure recovery transitions."""
    INITIALIZING = "initializing"
    READY = "ready"
    TRADING = "trading"
    SYNCING = "syncing"
    DEGRADED = "degraded"
    OFFLINE = "offline"

@dataclass
class NodeConfig:
    """Immutable node configuration ensuring consistency across network."""
    node_id: str
    max_position_size: float
    allowed_markets: List[str]
    consensus_threshold: float = 0.66
    heartbeat_interval: int = 30  # seconds
    
    def __post_init__(self):
        """Validate configuration on initialization."""
        if self.max_position_size <= 0:
            raise ValueError("max_position_size must be positive")
        if not 0.5 <= self.consensus_threshold <= 1.0:
            raise ValueError("consensus_threshold must be between 0.5 and 1.0")

class AutonomousNode:
    """Autonomous trading node with self-healing capabilities."""
    
    def __init__(self, config: NodeConfig, firebase_app):
        self.config = config
        self.status = NodeStatus.INITIALIZING
        self.db = firestore.client(firebase_app)
        self.rtdb = db.reference()
        self.performance_metrics: Dict = {}
        self._health_check_task = None
        self._consensus_group: List[str] = []
        
        # Initialize performance tracking
        self._init_performance_tracking()
        logger.info("Node initialized", node_id=config.node_id)
    
    def _init_performance_tracking(self):
        """Initialize performance metrics with default values."""
        self.performance_metrics = {
            "trades_executed": 0,
            "successful_trades": 0,
            "total_pnl": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "last_updated": datetime.utcnow().isoformat()
        }
    
    async def start(self):
        """Start node with graceful failure handling."""
        try:
            await self._establish_network_presence()
            await self._join_consensus_group()
            await self._start_health_monitoring()
            self.status = NodeStatus.READY
            logger.info("Node started successfully", node_id=self.config.node_id)
        except Exception as e:
            logger.error("Node startup failed", error=str(e), node_id=self.config.node_id)
            self.status = NodeStatus.DEGRADED
            await self._attempt_recovery()
    
    async def _establish_network_presence(self):
        """Register node in network registry with conflict resolution."""
        node_ref = self.db.collection("nodes").document(self.config.node_id)
        
        # Check for existing node with same ID (zombie node detection)
        existing = node_ref.get()
        if existing.exists:
            last_seen = existing.get("last_heartbeat")
            if datetime.utcnow() - last_seen > timedelta(minutes=5):
                logger.warning("Removing stale node registration", node_id=self.config.node_id)
                # Proceed with registration
            else:
                raise ValueError(f"Node ID {self.config.node_id} already active")
        
        node_data = {
            "node_id": self.config.node_id,
            "status": self.status.value,
            "config": self.config.__dict__,
            "registered_at": firestore.SERVER_TIMESTAMP,
            "last_heartbeat": firestore.SERVER_TIMESTAMP,
            "performance": self.performance_metrics
        }
        
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: node_ref.set(node_data)
        )
    
    async def _join_consensus_group(self):
        """Join decentralized consensus group with load balancing."""
        consensus_ref = self.rtdb.child("consensus/groups")
        
        # Find group with lowest load (simple load balancing)
        groups = await asyncio.get_event_loop().run_in_executor(
            None, lambda: consensus_ref.get()
        ) or {}
        
        target_group = None
        min_load = float('inf')
        
        for group_id, group_data in groups.items():
            node_count