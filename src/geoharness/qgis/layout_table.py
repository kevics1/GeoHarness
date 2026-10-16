"""Editable, source-bound attribute rosters, measured with native QGIS tables."""

from __future__ import annotations

import json
import math


def select_roster(records, spec):
    """Select explicitly; keep original records intact for complete-roster export."""
    records = list(records)
    key = spec["fields"][0]
    if spec.get("keys") is not None:
        wanted = {json.dumps(value, sort_keys=True) for value in spec["keys"]}
        found = {json.dumps(record[key], sort_keys=True) for record in records}
        if not wanted <= found:
            raise ValueError("Roster keys include values absent from the source")
        records = [record for record in records
                   if json.dumps(record[key], sort_keys=True) in wanted]
    if spec.get("sort_by"):
        field = spec["sort_by"]
        if any(field not in record for record in records):
            raise ValueError(f"Roster sort_by field is missing: {field}")
        try:
            known = [row for row in records if row[field] is not None]
            missing = [row for row in records if row[field] is None]
            known.sort(key=lambda row: (row[field], str(row[key])),
                       reverse=spec.get("descending", False))
            records = known + missing
        except TypeError as exc:
            raise ValueError("Roster sort_by has incompatible mixed values") from exc
    if spec.get("top_n") is not None:
        records = records[:spec["top_n"]]
    return records


def add_label_tables(layout, layers, spec, regions, text_format, *, page_spec=None, context=None):
    from qgis.core import (
        NULL,
        QgsExpression,
        QgsLayoutFrame,
        QgsLayoutItemAttributeTable,
        QgsLayoutItemPage,
        QgsLayoutMultiFrame,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsLayoutTable,
        QgsLayoutTableColumn,
        QgsVectorLayer,
    )

    layer = layers[spec["layer_index"]]
    fields = spec["fields"]
    if not isinstance(layer, QgsVectorLayer) or any(
        field not in layer.fields().names() for field in fields
    ) or len(set(fields)) != len(fields):
        raise ValueError("label_table requires distinct existing vector attributes")
    values = [feature[fields[0]] for feature in layer.getFeatures()]
    identities = [json.dumps(value, default=str) for value in values]
    if (any(value == NULL or value is None for value in values)
            or len(set(identities)) != len(values)):
        raise ValueError("label_table first field must be a unique non-null roster key")
    records = [{field.name(): (None if feature[field.name()] == NULL else feature[field.name()])
                for field in layer.fields()} for feature in layer.getFeatures()]
    selected_records = select_roster(records, spec)
    values = [record[fields[0]] for record in selected_records]
    if context is not None:
        import csv

        # Native fitting may rebuild the layout within this fresh run. The source
        # snapshot is unchanged; create the complete roster just once.
        if not (context.run_dir / "roster_full.csv").exists():
            target = context.output_path(None, "roster_full.csv")
            with open(target, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=layer.fields().names())
                writer.writeheader()
                writer.writerows(records)
    layout.setCustomProperty("geoharness/roster_selection", json.dumps({
        "source_rows": len(records), "selected_rows": len(values), "keys": values,
        "full_roster": "roster_full.csv" if context is not None else None}, default=str))
    count = spec.get("columns", 3)
    paginated = spec.get("placement") == "pages"
    first_roster_page = layout.pageCollection().pageCount()
    chunk = max(1, math.ceil(len(values) / count))
    pending = list(values)
    index = 0
    while pending:
        selected = pending if paginated else pending[:chunk]
        if not selected:
            continue
        page_number = 0
        if paginated:
            column = index % count
            page_number = index // count + first_roster_page
            if column == 0:
                page = QgsLayoutItemPage(layout)
                page.setPageSize(QgsLayoutSize(page_spec.get("width_mm", 297),
                                              page_spec.get("height_mm", 210)))
                layout.pageCollection().addPage(page)
            margin, gap = page_spec.get("margin_mm", 12), page_spec.get("gap_mm", 5)
            width = (page_spec.get("width_mm", 297) - 2 * margin - gap * (count - 1)) / count
            rect = [margin + column * (width + gap), margin, width,
                    page_spec.get("height_mm", 210) - 2 * margin]
        else:
            rect = regions[f"label_table_{index}"]
        table = QgsLayoutItemAttributeTable(layout)
        table.setCustomProperty("geoharness/table_id", f"label_table_{index}")
        table.setCustomProperty("geoharness/page", page_number)
        table.setVectorLayer(layer)
        table.setDisplayedFields(fields)
        table.setMaximumNumberOfFeatures(len(selected))
        table.setFilterFeatures(True)
        expression = QgsExpression.quotedColumnRef(fields[0]) + " IN (" + ",".join(
            QgsExpression.quotedValue(value) for value in selected) + ")"
        table.setFeatureFilter(expression)
        # Keep the native editable table in exactly the selected record order,
        # including tie breaks and explicit selections across page boundaries.
        from qgis.PyQt.QtCore import Qt

        ordering = QgsLayoutTableColumn()
        ordering.setAttribute('CASE '
                              + ' '.join('WHEN ' + QgsExpression.quotedColumnRef(fields[0])
                                         + ' = ' + QgsExpression.quotedValue(value)
                                         + f' THEN {rank}' for rank, value in enumerate(selected))
                              + ' END')
        sort_expression = QgsExpression(ordering.attribute())
        if sort_expression.hasParserError():
            raise ValueError('Roster ordering expression is invalid: '
                             + sort_expression.parserErrorString())
        ordering.setSortOrder(Qt.AscendingOrder)
        table.setSortColumns([ordering])
        table.setContentTextFormat(text_format)
        table.setHeaderTextFormat(text_format)
        table.setCellMargin(1)
        table.setWrapBehavior(QgsLayoutTable.WrapText)
        columns = table.columns()
        decoration = 2 * table.cellMargin() + (
            (len(columns) + 1) * table.gridStrokeWidth() / len(columns))
        weights = spec.get("field_widths", {})
        total_weight = sum(weights.get(field, 1) for field in fields)
        for column in columns:
            column.setWidth(max(1, rect[2] * weights.get(column.attribute(), 1)
                                / total_weight - decoration))
            column.setHeading(spec.get("headers", {}).get(column.attribute(), column.heading()))
            if column.attribute() in spec.get("precision", {}):
                places = spec["precision"][column.attribute()]
                column.setAttribute("round(" + QgsExpression.quotedColumnRef(column.attribute())
                                    + f", {places})")
        table.setColumns(columns)
        table.setResizeMode(QgsLayoutMultiFrame.UseExistingFrames)
        layout.addMultiFrame(table)
        frame = QgsLayoutFrame(layout, table)
        frame.setId(f"label_table_{index}")
        frame.attemptMove(QgsLayoutPoint(rect[0], rect[1]), False, True, page_number)
        frame.attemptResize(QgsLayoutSize(rect[2], rect[3]))
        table.addFrame(frame)
        table.refreshAttributes()
        if paginated:
            from qgis.core import QgsLayoutUtils

            render = QgsLayoutUtils.createRenderContextForLayout(
                layout, None, layout.renderContext().dpi())
            capacity = table.rowsVisible(render, 0, 0, False)
            if capacity < 1:
                error = ValueError("A single roster row cannot fit on the roster page")
                error.diagnostics = {"code": "roster_row_too_tall", "available_mm": rect,
                    "required_height_mm": table.totalSize().height(), "fields": fields,
                    "remedy": "Increase layout.width_mm or field_widths for long text"}
                raise error
            selected = selected[:capacity]
            table.setMaximumNumberOfFeatures(len(selected))
            table.setFeatureFilter(QgsExpression.quotedColumnRef(fields[0]) + " IN (" + ",".join(
                QgsExpression.quotedValue(value) for value in selected) + ")")
            table.refreshAttributes()
        pending = pending[len(selected):]
        index += 1


def label_table_facts(layout):
    from qgis.core import QgsLayoutItemAttributeTable, QgsLayoutUtils

    context = QgsLayoutUtils.createRenderContextForLayout(
        layout, None, layout.renderContext().dpi())
    result = []
    for table in layout.multiFrames():
        if not isinstance(table, QgsLayoutItemAttributeTable) or not table.customProperty(
            "geoharness/table_id", ""
        ):
            continue
        table.refreshAttributes()
        contents = table.contents()
        capacity = table.rowsVisible(context, 0, 0, False)
        result.append({"id": table.customProperty("geoharness/table_id"),
            "page": int(table.customProperty("geoharness/page", 0)),
            "fields": [column.attribute() for column in table.columns()],
            "headers": [column.heading() for column in table.columns()],
            "rows": len(contents), "visible_rows": min(len(contents), capacity),
            "required_height_mm": table.totalSize().height(),
            "available_mm": [[frame.rect().width(), frame.rect().height()]
                             for frame in table.frames()],
            "contents": [[value if type(value) in {str, int, float, bool} or value is None
                          else str(value) for value in row] for row in contents]})
    return sorted(result, key=lambda item: item["id"])


def export_full_roster(layout, context):
    """Rebuild a complete roster from the actual saved table source on export."""
    import csv

    from qgis.core import NULL, QgsLayoutItemAttributeTable

    table = next((table for table in layout.multiFrames()
                  if isinstance(table, QgsLayoutItemAttributeTable)
                  and table.customProperty('geoharness/table_id', '')), None)
    if table is None or table.vectorLayer() is None:
        return None
    layer = table.vectorLayer()
    target = context.run_dir / 'roster_full.csv'
    if not target.exists():
        target = context.output_path(None, 'roster_full.csv')
        with open(target, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(layer.fields().names())
            for feature in layer.getFeatures():
                writer.writerow([None if feature[field.name()] == NULL else feature[field.name()]
                                 for field in layer.fields()])
    return str(target)
