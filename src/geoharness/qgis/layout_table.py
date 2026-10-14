"""Editable, source-bound attribute rosters, measured with native QGIS tables."""

from __future__ import annotations

import json
import math


def add_label_tables(layout, layers, spec, regions, text_format):
    from qgis.core import (
        NULL,
        QgsExpression,
        QgsLayoutFrame,
        QgsLayoutItemAttributeTable,
        QgsLayoutMultiFrame,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsLayoutTable,
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
    count = spec.get("columns", 3)
    chunk = max(1, math.ceil(len(values) / count))
    for index in range(count):
        selected = values[index * chunk:(index + 1) * chunk]
        if not selected:
            continue
        rect = regions[f"label_table_{index}"]
        table = QgsLayoutItemAttributeTable(layout)
        table.setCustomProperty("geoharness/table_id", f"label_table_{index}")
        table.setVectorLayer(layer)
        table.setDisplayedFields(fields)
        table.setMaximumNumberOfFeatures(len(selected))
        table.setFilterFeatures(True)
        expression = QgsExpression.quotedColumnRef(fields[0]) + " IN (" + ",".join(
            QgsExpression.quotedValue(value) for value in selected) + ")"
        table.setFeatureFilter(expression)
        table.setContentTextFormat(text_format)
        table.setHeaderTextFormat(text_format)
        table.setCellMargin(1)
        table.setWrapBehavior(QgsLayoutTable.WrapText)
        columns = table.columns()
        decoration = 2 * table.cellMargin() + (
            (len(columns) + 1) * table.gridStrokeWidth() / len(columns))
        for column in columns:
            column.setWidth(max(1, rect[2] / len(columns) - decoration))
            column.setHeading(spec.get("headers", {}).get(column.attribute(), column.heading()))
        table.setColumns(columns)
        table.setResizeMode(QgsLayoutMultiFrame.UseExistingFrames)
        layout.addMultiFrame(table)
        frame = QgsLayoutFrame(layout, table)
        frame.setId(f"label_table_{index}")
        frame.attemptMove(QgsLayoutPoint(rect[0], rect[1]))
        frame.attemptResize(QgsLayoutSize(rect[2], rect[3]))
        table.addFrame(frame)
        table.refreshAttributes()


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
            "fields": [column.attribute() for column in table.columns()],
            "headers": [column.heading() for column in table.columns()],
            "rows": len(contents), "visible_rows": min(len(contents), capacity),
            "contents": [[value if type(value) in {str, int, float, bool} or value is None
                          else str(value) for value in row] for row in contents]})
    return sorted(result, key=lambda item: item["id"])
