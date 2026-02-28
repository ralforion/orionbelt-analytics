"""
Data Cleanup Functions

Implements automatic cleanup of old GraphRAG and RDF ontology versions
based on retention policies.
"""

import logging
import shutil
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

from .metadata import VersionMetadataManager, VersionInfo

logger = logging.getLogger(__name__)


class DataCleanupManager:
    """
    Manages cleanup of old GraphRAG and RDF data based on retention policies.
    """

    def __init__(self, connection_id: str, output_dir: Path):
        """
        Initialize cleanup manager.

        Args:
            connection_id: Database connection fingerprint
            output_dir: Base output directory (usually tmp/)
        """
        self.connection_id = connection_id
        self.output_dir = output_dir
        self.connection_dir = output_dir / connection_id
        self.metadata_mgr = VersionMetadataManager(connection_id, output_dir)

    def cleanup_graphrag(
        self,
        schema_name: str,
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        Clean up old GraphRAG data based on retention policy.

        Args:
            schema_name: Schema name
            dry_run: If True, only report what would be deleted

        Returns:
            Cleanup report
        """
        versions_to_delete = self.metadata_mgr.get_versions_to_cleanup(
            schema_name,
            data_type="graphrag"
        )

        if not versions_to_delete:
            return {
                "deleted": [],
                "kept_all": True,
                "reason": "All versions within retention policy"
            }

        deleted = []
        errors = []

        for version in versions_to_delete:
            try:
                if not dry_run:
                    # Delete ChromaDB collection or JSON files
                    self._delete_graphrag_files(schema_name, version.version)

                    # Mark as deleted in metadata
                    self.metadata_mgr.mark_version_deleted(
                        schema_name,
                        version.version,
                        "graphrag"
                    )

                age_days = (datetime.now() - datetime.fromisoformat(version.created_at)).days

                deleted.append({
                    "version": version.version,
                    "age_days": age_days,
                    "created_at": version.created_at,
                    "reason": f"Age {age_days} days exceeds max {self.metadata_mgr.get_retention_policy().graphrag_max_age_days} days"
                })

            except Exception as e:
                logger.error(f"Failed to delete GraphRAG version {version.version}: {e}")
                errors.append({
                    "version": version.version,
                    "error": str(e)
                })

        return {
            "deleted": deleted,
            "errors": errors,
            "dry_run": dry_run
        }

    def cleanup_ontology(
        self,
        schema_name: str,
        dry_run: bool = True,
        oxigraph_store: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Clean up old RDF ontology data based on retention policy.

        Args:
            schema_name: Schema name
            dry_run: If True, only report what would be deleted
            oxigraph_store: OxigraphStoreManager instance for deleting graphs

        Returns:
            Cleanup report
        """
        versions_to_delete = self.metadata_mgr.get_versions_to_cleanup(
            schema_name,
            data_type="ontology"
        )

        if not versions_to_delete:
            return {
                "deleted": [],
                "kept_all": True,
                "reason": "All versions within retention policy"
            }

        deleted = []
        errors = []

        for version in versions_to_delete:
            try:
                if not dry_run:
                    # Delete TTL file
                    ttl_path = self.output_dir / version.ontology_ttl_file
                    if ttl_path.exists():
                        ttl_path.unlink()

                    # Delete named graph from Oxigraph (if store provided)
                    if oxigraph_store and version.ontology_graph_uri:
                        try:
                            oxigraph_store.delete_graph(version.ontology_graph_uri)
                        except Exception as e:
                            logger.warning(f"Failed to delete graph {version.ontology_graph_uri}: {e}")

                    # Mark as deleted in metadata
                    self.metadata_mgr.mark_version_deleted(
                        schema_name,
                        version.version,
                        "ontology"
                    )

                age_days = (datetime.now() - datetime.fromisoformat(version.created_at)).days

                deleted.append({
                    "version": version.version,
                    "age_days": age_days,
                    "created_at": version.created_at,
                    "graph_uri": version.ontology_graph_uri,
                    "ttl_file": version.ontology_ttl_file,
                    "reason": f"Age {age_days} days exceeds max {self.metadata_mgr.get_retention_policy().ontology_max_age_days} days"
                })

            except Exception as e:
                logger.error(f"Failed to delete Ontology version {version.version}: {e}")
                errors.append({
                    "version": version.version,
                    "error": str(e)
                })

        return {
            "deleted": deleted,
            "errors": errors,
            "dry_run": dry_run
        }

    def cleanup_all(
        self,
        schema_name: str,
        dry_run: bool = True,
        oxigraph_store: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        Clean up both GraphRAG and Ontology data.

        Args:
            schema_name: Schema name
            dry_run: If True, only report what would be deleted
            oxigraph_store: OxigraphStoreManager instance

        Returns:
            Combined cleanup report
        """
        graphrag_report = self.cleanup_graphrag(schema_name, dry_run)
        ontology_report = self.cleanup_ontology(schema_name, dry_run, oxigraph_store)

        return {
            "graphrag": graphrag_report,
            "ontology": ontology_report,
            "dry_run": dry_run
        }

    def _delete_graphrag_files(self, schema_name: str, version: int):
        """
        Delete GraphRAG files for a specific version.

        Args:
            schema_name: Schema name
            version: Version number
        """
        # Check for ChromaDB collection
        chromadb_dir = self.output_dir / "chromadb" / self.connection_id
        if chromadb_dir.exists():
            # ChromaDB collections are managed by ChromaDB itself
            # We'll need to use the ChromaDB client to delete collections
            # For now, just log
            logger.info(f"ChromaDB collection cleanup for version {version} (managed by ChromaDB)")

        # Check for JSON files (legacy or backup)
        json_patterns = [
            f"vector_store_{schema_name}_v{version}.json",
            f"graph_{schema_name}_v{version}.json",
            f"communities_{schema_name}_v{version}.json"
        ]

        for pattern in json_patterns:
            file_path = self.connection_dir / pattern
            if file_path.exists():
                file_path.unlink()
                logger.info(f"Deleted {file_path}")

    def get_cleanup_recommendations(
        self,
        schema_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get recommendations for cleanup without actually deleting.

        Args:
            schema_name: Specific schema name, or None for all schemas

        Returns:
            Cleanup recommendations
        """
        recommendations = []

        schemas_to_check = (
            [schema_name] if schema_name
            else list(self.metadata_mgr.metadata.get("schemas", {}).keys())
        )

        total_versions_to_delete = 0
        estimated_space_to_free = 0

        for schema in schemas_to_check:
            graphrag_versions = self.metadata_mgr.get_versions_to_cleanup(
                schema,
                "graphrag"
            )
            ontology_versions = self.metadata_mgr.get_versions_to_cleanup(
                schema,
                "ontology"
            )

            if graphrag_versions or ontology_versions:
                recommendations.append({
                    "schema": schema,
                    "graphrag_deletable": len(graphrag_versions),
                    "ontology_deletable": len(ontology_versions),
                    "oldest_version_age": self._get_oldest_age(
                        graphrag_versions + ontology_versions
                    )
                })

                total_versions_to_delete += len(graphrag_versions) + len(ontology_versions)

        return {
            "recommendations": recommendations,
            "total_versions_to_delete": total_versions_to_delete,
            "estimated_space_to_free_mb": estimated_space_to_free,
            "retention_policy": self.metadata_mgr.get_retention_policy().__dict__
        }

    def _get_oldest_age(self, versions: List[VersionInfo]) -> int:
        """Get age of oldest version in days."""
        if not versions:
            return 0

        now = datetime.now()
        oldest = min(
            datetime.fromisoformat(v.created_at)
            for v in versions
        )

        return (now - oldest).days
