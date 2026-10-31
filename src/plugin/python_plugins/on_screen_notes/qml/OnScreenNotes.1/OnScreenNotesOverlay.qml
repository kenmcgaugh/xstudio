// SPDX-License-Identifier: Apache-2.0
import QtQuick.Controls.Basic
import QtQuick.Layouts
import QtQuick
import QuickFuture
import QuickPromise

// These imports are necessary to have access to custom QML types that are
// part of the xSTUDIO UI implementation.
import xStudio 1.0
import xstudio.qml.models 1.0

Item {

    id: root
    width: visible ? thetext.implicitWidth : 0
    height: visible ? thetext.implicitHeight : 0
    x: offset_x * parent.width
    y: offset_y * parent.height

    visible: note_text && note_text != "undefined" && opacity != 0.0

    property var note_text: "" + media_item_hud_data

    Rectangle {
        color: "black"
        opacity: 0.5
        anchors.fill: parent
    }

    XsText {
        id: thetext
        //anchors.fill: parent
        font.pixelSize: Math.round(font_size*scalingFactor)
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        text: note_text ? note_text : ""
        color: font_colour
    }

    XsModuleData {
        id: onscreen_attrs
        modelDataName: "on_screen_notes"
    }

    XsAttributeValue {
        id: __font_colour
        attributeTitle: "Font Colour"
        model: onscreen_attrs
    }
    property alias font_colour: __font_colour.value

    XsAttributeValue {
        id: __offset_x
        attributeTitle: "Offset X"
        model: onscreen_attrs
    }
    property alias offset_x: __offset_x.value

    XsAttributeValue {
        id: __offset_y
        attributeTitle: "Offset Y"
        model: onscreen_attrs
    }
    property alias offset_y: __offset_y.value

    XsAttributeValue {
        id: __the_text
        attributeTitle: "Display Text"
        model: onscreen_attrs
    }
    property alias display_text: __the_text.value

    XsAttributeValue {
        id: __font_size
        attributeTitle: "Font Size"
        model: onscreen_attrs
    }
    property alias font_size: __font_size.value
}
