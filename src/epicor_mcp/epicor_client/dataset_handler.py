"""Dataset (ds) workflow handler for Epicor Kinetic write operations.

Epicor's create, update, and delete operations follow a multi-step "dataset"
pattern.  This module automates that pattern so callers don't have to manage
the intermediate ``ds`` state manually.

Typical flows:
    - **Create**: ``GetNew{Entity}`` -> modify row -> ``Update``
    - **Update**: ``GetByID`` -> set ``RowMod="U"`` -> modify row -> ``Update``
    - **Delete**: ``GetByID`` -> set ``RowMod="D"`` -> ``Update``

Critical rules (from Epicor skill docs):
    - Always pass the **updated** ``ds`` from each step to the next step.
    - ``RowMod`` values: ``"A"`` = add, ``"U"`` = update, ``"D"`` = delete.
    - Date format: always ``"YYYY-MM-DDT00:00:00"``.
    - Response data may live in ``returnObj`` **or** ``parameters.ds`` -- check both.
    - After a ``Change*`` method, the ds may have different values than what
      you set (calculated fields, defaults, etc.).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from epicor_mcp.epicor_client.http_client import EpicorClient
    from epicor_mcp.index.service_index import ServiceIndex

logger = logging.getLogger(__name__)


class DatasetHandler:
    """Handles Epicor dataset (ds) workflow patterns for create, update, and delete operations.

    This class is **stateless**: the dataset flows through method parameters
    rather than being stored as instance state.  This makes it safe to reuse
    a single ``DatasetHandler`` across concurrent requests.

    Parameters
    ----------
    client:
        ``EpicorClient`` instance used to issue HTTP requests to the
        Epicor Kinetic REST API.
    index:
        ``ServiceIndex`` instance used to look up available methods
        (e.g., checking whether a ``Change{Field}`` method exists).

    Examples
    --------
    Create a PO header::

        handler = DatasetHandler(client, index)
        result = await handler.create_record(
            base_url="https://epicor.example.com/YourInstance/api/v2/odata/DEMO/",
            service="Erp.BO.POSvc",
            entity="POHeader",
            api_key="abc123",
            changes={"VendorNum": 1234, "BuyerID": "JSmith"},
        )

    Update a vendor::

        result = await handler.update_record(
            base_url="https://epicor.example.com/YourInstance/api/v2/odata/DEMO/",
            service="Erp.BO.VendorSvc",
            api_key="abc123",
            record_id={"vendorNum": 5678},
            entity="Vendor",
            changes={"Name": "New Vendor Name"},
        )
    """

    def __init__(self, client: "EpicorClient", index: "ServiceIndex") -> None:
        self._client = client
        self._index = index

    # ------------------------------------------------------------------
    # Public workflow methods
    # ------------------------------------------------------------------

    async def create_record(
        self,
        base_url: str,
        service: str,
        entity: str,
        api_key: str,
        changes: dict[str, Any],
    ) -> dict:
        """Create a new record using the ``GetNew`` -> modify -> ``Update`` pattern.

        Args:
            base_url: Epicor OData base URL
                (e.g., ``"https://host/SaaS/api/v2/odata/DEMO/"``).
            service: Full service name (e.g., ``"Erp.BO.POSvc"``).
            entity: Entity/table name for GetNew (e.g., ``"POHeader"``).
            api_key: Department API key for ``X-API-Key`` header.
            changes: Dict of ``field_name -> value`` to set on the new record.

        Returns:
            The committed dataset from the ``Update`` call.

        Raises:
            Exception: Propagated from the HTTP client if any step fails.
                The exception message includes which step failed and the
                current state of the dataset for debugging.
        """
        getnew_method = self.find_getnew_method(service, entity)
        step = getnew_method

        try:
            # Step 1: Call GetNew{entity} to get a blank dataset row
            logger.info("create_record: calling %s/%s", service, getnew_method)
            getnew_url = f"{service}/{getnew_method}"
            getnew_body: dict[str, Any] = {"ds": {}}
            response = await self._client.post(getnew_url, api_key, json_body=getnew_body)

            ds = self.extract_ds(response)
            if not ds:
                raise ValueError(
                    f"{getnew_method} returned an empty dataset. "
                    f"Full response: {response}"
                )

            # Step 2: Locate the new row in the entity table
            rows = ds.get(entity, [])
            if not rows:
                raise ValueError(
                    f"{getnew_method} did not return any rows in "
                    f"table '{entity}'. Dataset tables: {list(ds.keys())}"
                )

            # Step 3: Apply field changes, calling Change* methods where available
            step = "apply_changes"
            ds = await self._apply_changes_with_change_methods(
                base_url=base_url,
                service=service,
                api_key=api_key,
                ds=ds,
                entity=entity,
                changes=changes,
            )

            # Step 4: Ensure RowMod is still "A" (Change* methods might clear it)
            target_rows = ds.get(entity, [])
            if target_rows:
                target_rows[-1]["RowMod"] = target_rows[-1].get("RowMod") or "A"

            # Step 5: Call Update to commit
            step = "Update"
            logger.info("create_record: calling %s/Update", service)
            update_url = f"{service}/Update"
            update_response = await self._client.post(
                update_url, api_key, json_body={"ds": ds}
            )

            return self.extract_ds(update_response) or update_response

        except Exception as exc:
            logger.exception("create_record failed at step '%s'", step)
            raise type(exc)(
                f"create_record failed at step '{step}': {exc}"
            ) from exc

    async def update_record(
        self,
        base_url: str,
        service: str,
        api_key: str,
        record_id: dict[str, Any],
        entity: str,
        changes: dict[str, Any],
    ) -> dict:
        """Update an existing record using ``GetByID`` -> modify -> ``Update``.

        Args:
            base_url: Epicor OData base URL.
            service: Full service name (e.g., ``"Erp.BO.VendorSvc"``).
            api_key: Department API key.
            record_id: Primary key dict (e.g., ``{"poNum": 12345}``).
            entity: Which table in the dataset to modify (e.g., ``"POHeader"``).
            changes: Dict of ``field_name -> value`` to update.

        Returns:
            The committed dataset from the ``Update`` call.
        """
        step = "GetByID"

        try:
            # Step 1: Call GetByID with the primary key
            logger.info(
                "update_record: calling %s/GetByID with %s", service, record_id
            )
            getbyid_url = f"{service}/GetByID"
            response = await self._client.post(getbyid_url, api_key, json_body=record_id)

            ds = self.extract_ds(response)
            if not ds:
                raise ValueError(
                    f"GetByID returned an empty dataset for {record_id}. "
                    f"Full response: {response}"
                )

            # Step 2: Find the target row in the entity table
            rows = ds.get(entity, [])
            if not rows:
                raise ValueError(
                    f"GetByID did not return any rows in table '{entity}'. "
                    f"Dataset tables: {list(ds.keys())}. "
                    f"Record ID: {record_id}"
                )

            # Use the first row (GetByID returns the specific record).
            target_row = rows[0]

            # Step 3: Set RowMod to "U" for update
            target_row["RowMod"] = "U"

            # Step 4: Apply field changes, calling Change* methods where available
            step = "apply_changes"
            ds = await self._apply_changes_with_change_methods(
                base_url=base_url,
                service=service,
                api_key=api_key,
                ds=ds,
                entity=entity,
                changes=changes,
            )

            # Step 5: Ensure RowMod is still "U" after Change* calls
            target_rows = ds.get(entity, [])
            if target_rows:
                target_rows[0]["RowMod"] = target_rows[0].get("RowMod") or "U"

            # Step 6: Call Update to commit
            step = "Update"
            logger.info("update_record: calling %s/Update", service)
            update_url = f"{service}/Update"
            update_response = await self._client.post(
                update_url, api_key, json_body={"ds": ds}
            )

            return self.extract_ds(update_response) or update_response

        except Exception as exc:
            logger.exception("update_record failed at step '%s'", step)
            raise type(exc)(
                f"update_record failed at step '{step}': {exc}"
            ) from exc

    async def delete_record(
        self,
        base_url: str,
        service: str,
        api_key: str,
        record_id: dict[str, Any],
        entity: str,
    ) -> dict:
        """Delete a record using ``GetByID`` -> set ``RowMod "D"`` -> ``Update``.

        Args:
            base_url: Epicor OData base URL.
            service: Full service name.
            api_key: Department API key.
            record_id: Primary key dict (e.g., ``{"poNum": 12345}``).
            entity: Which table in the dataset to target (e.g., ``"POHeader"``).

        Returns:
            The dataset from the ``Update`` call (typically with the row removed).
        """
        step = "GetByID"

        try:
            # Step 1: Call GetByID to fetch the current record
            logger.info(
                "delete_record: calling %s/GetByID with %s", service, record_id
            )
            getbyid_url = f"{service}/GetByID"
            response = await self._client.post(getbyid_url, api_key, json_body=record_id)

            ds = self.extract_ds(response)
            if not ds:
                raise ValueError(
                    f"GetByID returned an empty dataset for {record_id}. "
                    f"Full response: {response}"
                )

            # Step 2: Find the row and mark it for deletion
            rows = ds.get(entity, [])
            if not rows:
                raise ValueError(
                    f"GetByID did not return any rows in table '{entity}'. "
                    f"Record ID: {record_id}"
                )

            rows[0]["RowMod"] = "D"

            # Step 3: Call Update to commit the deletion
            step = "Update"
            logger.info("delete_record: calling %s/Update", service)
            update_url = f"{service}/Update"
            update_response = await self._client.post(
                update_url, api_key, json_body={"ds": ds}
            )

            return self.extract_ds(update_response) or update_response

        except Exception as exc:
            logger.exception("delete_record failed at step '%s'", step)
            raise type(exc)(
                f"delete_record failed at step '{step}': {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _apply_changes_with_change_methods(
        self,
        base_url: str,
        service: str,
        api_key: str,
        ds: dict,
        entity: str,
        changes: dict[str, Any],
    ) -> dict:
        """Apply field changes to a dataset row, calling ``Change{Field}`` methods
        where they exist to trigger server-side validation and side effects.

        For each field in *changes*:

        1. Check if the service has a ``Change{Field}`` method (via the index).
        2. If **yes**: call it with ``{"Proposed{Field}": value, "ds": ds}``
           and use the **returned** ``ds`` going forward.
        3. If **no**: set the field directly on the row.

        This ensures that calculated fields, defaults, and cross-field
        validations are executed on the server whenever Epicor provides a
        ``Change*`` endpoint for the field.

        Args:
            base_url: Epicor OData base URL.
            service: Full service name.
            api_key: Department API key.
            ds: The current dataset (will not be mutated in place if a
                ``Change*`` method replaces it).
            entity: Table name containing the target row.
            changes: Dict of ``field_name -> value`` to apply.

        Returns:
            The final ``ds`` after all changes have been applied.

        Raises:
            Exception: If a ``Change*`` method call fails.  The exception
                includes the field name and the Epicor error message.
        """
        # Build a set of known method names for this service (for fast lookup).
        known_methods = self._get_method_names(service)

        # Find the target row -- use the last row for creates (GetNew appends),
        # first row for updates/deletes (GetByID returns the specific record).
        rows = ds.get(entity, [])
        if not rows:
            raise ValueError(
                f"No rows found in table '{entity}' while applying changes. "
                f"Dataset tables: {list(ds.keys())}"
            )

        # Determine if this is a new row (RowMod "A") to know which row to target.
        # For creates, the new row is typically the last one.
        # For updates, the target is the first row with RowMod "U".
        target_idx = self._find_target_row_index(rows)

        for field_name, value in changes.items():
            # Convention: Change method name matches "Change{FieldName}"
            change_method = f"Change{field_name}"

            if known_methods is not None and change_method in known_methods:
                # Call the Change* method with the proposed value
                logger.info(
                    "Calling %s/%s for field '%s' = %r",
                    service,
                    change_method,
                    field_name,
                    value,
                )
                change_url = f"{service}/{change_method}"
                change_body: dict[str, Any] = {
                    f"Proposed{field_name}": value,
                    "ds": ds,
                }

                try:
                    change_response = await self._client.post(
                        change_url, api_key, json_body=change_body
                    )
                    # The Change* method returns an updated ds.
                    updated_ds = self.extract_ds(change_response)
                    if updated_ds:
                        ds = updated_ds
                    else:
                        # Fallback: set field directly if response has no ds
                        logger.warning(
                            "%s returned no ds; setting field '%s' directly",
                            change_method,
                            field_name,
                        )
                        rows = ds.get(entity, [])
                        if rows:
                            rows[min(target_idx, len(rows) - 1)][field_name] = value
                except Exception:
                    # Log the error but also set the field directly so the
                    # caller can still attempt the Update.
                    logger.warning(
                        "Change method %s/%s failed; setting field '%s' directly",
                        service,
                        change_method,
                        field_name,
                        exc_info=True,
                    )
                    rows = ds.get(entity, [])
                    if rows:
                        rows[min(target_idx, len(rows) - 1)][field_name] = value
            else:
                # No Change* method -- set the field directly on the row.
                rows = ds.get(entity, [])
                if rows:
                    rows[min(target_idx, len(rows) - 1)][field_name] = value

        return ds

    def _get_method_names(self, service: str) -> set[str] | None:
        """Return the set of method names for *service*, or ``None`` if unknown.

        Uses the ``ServiceIndex`` to look up available methods.  Returns
        ``None`` (rather than an empty set) when the index has no data for
        the service, so the caller can distinguish "unknown service" from
        "service with no methods".
        """
        methods = self._index.get_methods(service)
        if not methods:
            return None
        return {m.get("method_name", "") for m in methods}

    @staticmethod
    def _find_target_row_index(rows: list[dict]) -> int:
        """Determine which row in *rows* is the target for field modifications.

        - If any row has ``RowMod == "A"`` (new record), return its index.
        - If any row has ``RowMod == "U"`` (update), return its index.
        - Otherwise, return ``0`` (first row).
        """
        for i, row in enumerate(rows):
            if row.get("RowMod") == "A":
                return i
        for i, row in enumerate(rows):
            if row.get("RowMod") == "U":
                return i
        return 0

    # ------------------------------------------------------------------
    # Static utility methods
    # ------------------------------------------------------------------

    @staticmethod
    def extract_ds(response: dict) -> dict:
        """Extract the dataset from an Epicor response.

        Epicor returns the dataset in different locations depending on the
        endpoint version and method type:

        - ``returnObj`` -- common for ``GetByID``, ``GetNew*``
        - ``parameters.ds`` -- common for ``Change*``, ``Update``

        This method checks ``returnObj`` first, then falls back to
        ``parameters.ds``.

        Args:
            response: The full JSON response from an Epicor API call.

        Returns:
            The extracted dataset dict.  Returns an empty dict if no
            dataset could be found in the response.
        """
        ds = response.get("returnObj")
        if ds is not None:
            return ds
        ds = response.get("parameters", {}).get("ds", {})
        return ds

    @staticmethod
    def find_getnew_method(service: str, entity: str) -> str:
        """Derive the ``GetNew`` method name for an entity.

        Follows Epicor's naming convention where the GetNew method for
        a table named ``"POHeader"`` is ``"GetNewPOHeader"``.

        Args:
            service: Service name (unused, reserved for future lookup logic).
            entity: Entity/table name (e.g., ``"POHeader"``, ``"APInvHed"``).

        Returns:
            The GetNew method name (e.g., ``"GetNewPOHeader"``).
        """
        return f"GetNew{entity}"

    @staticmethod
    def trim_dataset(ds: dict, target_entity: str | None = None) -> dict:
        """Remove empty child tables from a dataset response.

        Epicor datasets include all entity sets in the response, even
        when they contain no rows.  This strips empty lists to reduce
        response size for LLM consumption.

        The *target_entity* table is always preserved (even if empty)
        to confirm the operation target.  Non-list values (metadata
        like ``@odata.context``) are always preserved.

        Args:
            ds: The dataset dict from ``extract_ds()``.
            target_entity: Entity name to always keep in the result.

        Returns:
            A new dict with empty list values removed.
        """
        if not isinstance(ds, dict):
            return ds

        trimmed = {}
        for key, value in ds.items():
            # Always keep the target entity
            if key == target_entity:
                trimmed[key] = value
                continue
            # Keep non-list values (metadata fields)
            if not isinstance(value, list):
                trimmed[key] = value
                continue
            # Keep non-empty lists
            if value:
                trimmed[key] = value

        return trimmed
