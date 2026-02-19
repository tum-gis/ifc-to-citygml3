# IFC to CityGML 3.0 Converter
#
# Written by Thomas H. Kolbe (thomas.kolbe@tum.de), Chair of Geoinformatics, 
# School of Engineering and Design, Technical University of Munich
#
# Version 0.9.1, Last change: 2026-02-19
#
# This version supports multi-appearance/multi-texturing:
# - Multiple materials can be assigned to different faces of a geometry
# - Each surface gets a unique ID for targeted appearance mapping
# - Supports per-face colors from IFC (IfcIndexedColourMap, IfcMaterialConstituentSet)
# - Generates one Appearance element per CityGML feature with multiple X3DMaterial children

import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.element
import uuid
import numpy as np
import os
import argparse
from lxml import etree

# --- Namespaces for CityGML 3.0 ---
NSMAP = {
    'core': "http://www.opengis.net/citygml/3.0",
    'bldg': "http://www.opengis.net/citygml/building/3.0",
    'con': "http://www.opengis.net/citygml/construction/3.0",
    'gen': "http://www.opengis.net/citygml/generics/3.0",
    'app': "http://www.opengis.net/citygml/appearance/3.0",
    'gml': "http://www.opengis.net/gml/3.2",
    'xsi': "http://www.w3.org/2001/XMLSchema-instance",
    'xlink': "http://www.w3.org/1999/xlink"
}

# List of IFC Representation Types that imply a Volumetric Solid
# Reference: https://standards.buildingsmart.org/IFC/RELEASE/IFC4/ADD2_TC1/HTML/schema/ifcrepresentationresource/lexical/ifcshaperepresentation.htm
SOLID_REPRESENTATION_TYPES = {
    "SweptSolid",   # Extrusions, Revolutions
    "Brep",         # Boundary Representations (FacetedBrep)
    "AdvancedBrep", # NURBS / Advanced Solids
    "CSG",          # Constructive Solid Geometry
    "Clipping",     # Boolean results (Solid - Solid)
    "BoundingBox"   # Simplified solid box
}

class CityGMLGenerator:
    def __init__(self, input_path, output_path, no_references=False, reorient_shells=False, no_properties=False, georef_oktoberfest=False, list_unmapped_doors_windows=False, unrelated_doors_windows_in_dummy_bce=False, no_generic_attribute_sets=False, pset_names_as_prefixes=False, no_storeys=False, no_appearances=False, xoffset=0.0, yoffset=0.0, zoffset=0.0):
        """Initialize the CityGML generator with input/output paths and processing options."""
        self.input_path = input_path
        self.filename = os.path.basename(input_path)
        self.output_path = output_path
        self.no_references = no_references
        self.reorient_shells = reorient_shells
        # If true, force georeferencing to the center of Theresienwiese (Oktoberfest)
        self.georef_oktoberfest = georef_oktoberfest
        # If true, do not export property sets / generic attributes
        self.no_properties = no_properties
        # If true, list all unmapped doors and windows at the end
        self.list_unmapped_doors_windows = list_unmapped_doors_windows
        # If true, put unrelated doors and windows in a dummy BuildingConstructiveElement
        self.unrelated_doors_windows_in_dummy_bce = unrelated_doors_windows_in_dummy_bce
        # If true, output properties as direct generic attributes instead of GenericAttributeSets
        self.no_generic_attribute_sets = no_generic_attribute_sets
        # If true, prefix property names with their pset name
        self.pset_names_as_prefixes = pset_names_as_prefixes
        # If true, do not export CityGML Storey objects
        self.no_storeys = no_storeys
        # If true, do not export CityGML appearance elements (colors/materials)
        self.no_appearances = no_appearances
        # XYZ offsets to shift the model (applied after georeferencing)
        self.xoffset = xoffset
        self.yoffset = yoffset
        self.zoffset = zoffset
        self.model = ifcopenshell.open(input_path)
        
        self.settings = ifcopenshell.geom.settings()
        self.settings.set(self.settings.USE_WORLD_COORDS, True)
        self.settings.set("triangulation-type", ifcopenshell.ifcopenshell_wrapper.TRIANGLE_MESH)
        # Optionally enable shell reorientation to ensure consistent winding
        if getattr(self, 'reorient_shells', False):
            try:
                self.settings.set("reorient-shells", True)
            except Exception:
                pass

        print(f"Processing IFC file: {self.input_path} - IFC Version: {self.model.schema}")
        
        # Georeferencing parameters
        self.eastings = 0.0
        self.northings = 0.0
        self.orthogonal_height = 0.0
        self.scale = 1.0
        self.rotation_matrix = np.eye(3)
        self.srs_name = "EPSG:0"

        # Track gml:id values for each exported element so we can create xlinks
        # Maps IFC element -> gml:id string
        self.element_gml_ids = {}
        # Track which elements were actually exported (not removed later)
        # This is a set of IFC elements that have valid geometry and are in the output
        self.exported_elements = set()

        self._setup_georeferencing()

        # Optionally override georeferencing to Theresienwiese in Munich (location of the famous Oktoberfest)
        if getattr(self, 'georef_oktoberfest', False):
            # These are the UTM/ETRS coordinates for Theresienwiese in EPSG:25832
            self.eastings = 689738.0
            self.northings = 5334100.0
            self.orthogonal_height = 521.0
            self.srs_name = "EPSG:25832"
            print(f"Georeference set to Theresienwiese in Munich (EPSG:25832): E={self.eastings:.3f}, N={self.northings:.3f}, H={self.orthogonal_height}")

    def _setup_georeferencing(self):
        """Extract georeferencing parameters from IFC model (IfcMapConversion, IfcProjectedCRS)."""
        try:
            map_conversions = self.model.by_type("IfcMapConversion")
        except RuntimeError:
            map_conversions = []

        if map_conversions:
            mc = map_conversions[0]
            self.eastings = mc.Eastings
            self.northings = mc.Northings
            self.orthogonal_height = mc.OrthogonalHeight
            self.scale = mc.Scale if mc.Scale else 1.0

            if getattr(mc, 'XAxisAbscissa', None) is not None and getattr(mc, 'XAxisOrdinate', None) is not None:
                cos_r = mc.XAxisAbscissa
                sin_r = mc.XAxisOrdinate
                self.rotation_matrix = np.array([
                    [cos_r, -sin_r, 0],
                    [sin_r,  cos_r, 0],
                    [0,      0,     1]
                ])

            try:
                crs = self.model.by_type("IfcProjectedCRS")
            except RuntimeError:
                crs = []

            if crs and getattr(crs[0], 'Name', None):
                self.srs_name = crs[0].Name
        else:
            print("No IfcMapConversion found. Using local coordinates.")

    def transform_vertex(self, vertex):
        """Apply georeferencing transformation (scale, rotation, translation) to a vertex."""
        v = np.array(vertex) * self.scale
        v = np.dot(self.rotation_matrix, v)
        v[0] += self.eastings + self.xoffset
        v[1] += self.northings + self.yoffset
        v[2] += self.orthogonal_height + self.zoffset
        return v

    def create_external_reference(self, parent_element, ifc_guid):
        """Create a CityGML external reference linking to the original IFC element by GUID."""
        if getattr(self, 'no_references', False):
            return

        ext_ref = etree.SubElement(parent_element, f"{{{NSMAP['core']}}}externalReference")
        info = etree.SubElement(ext_ref, f"{{{NSMAP['core']}}}ExternalReference")
        target = etree.SubElement(info, f"{{{NSMAP['core']}}}targetResource")
        target.text = ifc_guid
        system = etree.SubElement(info, f"{{{NSMAP['core']}}}informationSystem")
        system.text = self.filename        

    def add_properties(self, city_object, ifc_element):
        """Add IFC property sets as CityGML generic attributes to a feature."""
        # Skip exporting properties when requested
        if getattr(self, 'no_properties', False):
            return
        psets = ifcopenshell.util.element.get_psets(ifc_element)
        
        # Check options
        no_generic_attribute_sets = getattr(self, 'no_generic_attribute_sets', False)
        pset_names_as_prefixes = getattr(self, 'pset_names_as_prefixes', False)
        
        for pset_name, properties in psets.items():
            if not properties or pset_name == 'id':
                continue
            # Filter properties to find valid ones (not None, not 'id')
            valid_props = {k: v for k, v in properties.items() if v is not None and k != 'id'}
            if not valid_props:
                continue

            if no_generic_attribute_sets:
                # Output properties as direct generic attributes without GenericAttributeSet wrapper
                for prop_name, prop_value in valid_props.items():
                    # Build attribute name with optional prefix
                    if pset_names_as_prefixes:
                        full_prop_name = f"[{pset_name}]{prop_name}"
                    else:
                        full_prop_name = prop_name
                    
                    self._add_generic_attribute(city_object, full_prop_name, prop_value)
            else:
                # Original behavior: use GenericAttributeSets
                gen_attr_container = etree.SubElement(city_object, f"{{{NSMAP['core']}}}genericAttribute")
                attr_set = etree.SubElement(gen_attr_container, f"{{{NSMAP['gen']}}}GenericAttributeSet")
                # encode the property set name as an element (CityGML requires element form)
                pset_name_el = etree.SubElement(attr_set, f"{{{NSMAP['gen']}}}name")
                pset_name_el.text = pset_name
                for prop_name, prop_value in valid_props.items():
                    inner_attr_container = etree.SubElement(attr_set, f"{{{NSMAP['gen']}}}genericAttribute")
                    self._add_generic_attribute_value(inner_attr_container, prop_name, prop_value, pset_names_as_prefixes, pset_name)

    def _add_generic_attribute(self, parent, attr_name, attr_value):
        """Helper method to add a generic attribute directly to parent element."""
        gen_attr_container = etree.SubElement(parent, f"{{{NSMAP['core']}}}genericAttribute")
        self._add_generic_attribute_value(gen_attr_container, attr_name, attr_value, False, None)

    def _add_generic_attribute_value(self, parent, attr_name, attr_value, add_prefix=False, pset_name=None):
        """Helper method to add the typed attribute value."""
        # Build full attribute name with optional prefix
        if add_prefix and pset_name:
            full_name = f"[{pset_name}]{attr_name}"
        else:
            full_name = attr_name
        
        # Booleans must be converted to integers (CityGML has no boolean generic type)
        if isinstance(attr_value, bool):
            attr = etree.SubElement(parent, f"{{{NSMAP['gen']}}}IntAttribute")
            an = etree.SubElement(attr, f"{{{NSMAP['gen']}}}name")
            an.text = full_name
            val = etree.SubElement(attr, f"{{{NSMAP['gen']}}}value")
            val.text = '1' if attr_value else '0'
        elif isinstance(attr_value, float):
            attr = etree.SubElement(parent, f"{{{NSMAP['gen']}}}DoubleAttribute")
            an = etree.SubElement(attr, f"{{{NSMAP['gen']}}}name")
            an.text = full_name
            val = etree.SubElement(attr, f"{{{NSMAP['gen']}}}value")
            val.text = str(attr_value)
        elif isinstance(attr_value, int):
            attr = etree.SubElement(parent, f"{{{NSMAP['gen']}}}IntAttribute")
            an = etree.SubElement(attr, f"{{{NSMAP['gen']}}}name")
            an.text = full_name
            val = etree.SubElement(attr, f"{{{NSMAP['gen']}}}value")
            val.text = str(attr_value)
        else:
            attr = etree.SubElement(parent, f"{{{NSMAP['gen']}}}StringAttribute")
            an = etree.SubElement(attr, f"{{{NSMAP['gen']}}}name")
            an.text = full_name
            val = etree.SubElement(attr, f"{{{NSMAP['gen']}}}value")
            val.text = str(attr_value)

    def get_doors_and_windows_in_element(self, element):
        """
        Finds all IfcDoor and IfcWindow elements that are contained within any building element.
        In IFC, doors/windows are related to elements via IfcOpeningElement:
        Element -> IfcRelVoidsElement -> IfcOpeningElement -> IfcRelFillsElement -> Door/Window
        This works for walls, slabs, roofs, curtain walls, and other constructive elements.
        """
        doors_and_windows = []
        
        # Check if the element has any voids (openings)
        if hasattr(element, 'HasOpenings') and element.HasOpenings:
            for rel_voids in element.HasOpenings:
                if rel_voids.is_a('IfcRelVoidsElement'):
                    opening = rel_voids.RelatedOpeningElement
                    if opening and opening.is_a('IfcOpeningElement'):
                        # Check if the opening is filled with a door or window
                        if hasattr(opening, 'HasFillings') and opening.HasFillings:
                            for rel_fills in opening.HasFillings:
                                if rel_fills.is_a('IfcRelFillsElement'):
                                    filling = rel_fills.RelatedBuildingElement
                                    if filling and (filling.is_a('IfcDoor') or filling.is_a('IfcWindow')):
                                        doors_and_windows.append(filling)
        
        return doors_and_windows

    def _add_door_or_window_as_filling(self, parent_element, door_or_window, building_appearance_count):
        """
        Adds a Door or Window as a filling element (con:filling) to the parent element.
        This is the proper CityGML 3.0 way to represent openings in constructive elements.
        Also adds appearance information if the door/window has colors/materials.
        Returns the number of materials added for appearance tracking.
        """
        dw_type = "Door" if door_or_window.is_a("IfcDoor") else "Window"
        materials_added = 0
        
        # Create filling element as child of the parent (Construction module)
        dw_prop = etree.SubElement(parent_element, f"{{{NSMAP['con']}}}filling")
        if dw_type == "Door":
            dw_elem = etree.SubElement(dw_prop, f"{{{NSMAP['con']}}}Door")
        else:
            dw_elem = etree.SubElement(dw_prop, f"{{{NSMAP['con']}}}Window")

        # Add metadata to door/window
        if hasattr(door_or_window, 'Description') and door_or_window.Description:
            desc_elem = etree.SubElement(dw_elem, f"{{{NSMAP['gml']}}}description")
            desc_elem.text = door_or_window.Description
        if hasattr(door_or_window, 'Name') and door_or_window.Name:
            name_elem = etree.SubElement(dw_elem, f"{{{NSMAP['gml']}}}name")
            name_elem.text = door_or_window.Name

        self.create_external_reference(dw_elem, getattr(door_or_window, 'GlobalId', 'UNKNOWN'))
        
        # Generate geometry with surface IDs and per-face materials for multi-appearance support
        dw_is_solid = self.is_intended_solid(door_or_window)
        dw_polygons, dw_surface_ids, dw_face_materials = self.get_geometry_with_surface_ids(door_or_window)
        dw_geometry_id = f"UUID_{uuid.uuid4()}" if dw_polygons else None
        
        # Add appearance if door/window has color information (before generic attributes)
        # Pass surface_ids and face_materials for per-face targeting
        if dw_geometry_id:
            success, mat_count = self.add_appearance(dw_elem, door_or_window, f"DW_{id(door_or_window)}", dw_geometry_id, dw_surface_ids, dw_face_materials)
            if success:
                materials_added = mat_count
        
        # Add generic attributes (after appearance)
        self.add_properties(dw_elem, door_or_window)

        # Output geometry (after generic attributes)
        if dw_polygons:
            if dw_is_solid:
                dw_lod3 = etree.SubElement(dw_elem, f"{{{NSMAP['core']}}}lod3Solid")
                dw_solid = etree.SubElement(dw_lod3, f"{{{NSMAP['gml']}}}Solid", attrib={f"{{{NSMAP['gml']}}}id": dw_geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                dw_exterior = etree.SubElement(dw_solid, f"{{{NSMAP['gml']}}}exterior")
                dw_shell = etree.SubElement(dw_exterior, f"{{{NSMAP['gml']}}}Shell")
                dw_parent = dw_shell
            else:
                dw_lod3 = etree.SubElement(dw_elem, f"{{{NSMAP['core']}}}lod3MultiSurface")
                dw_ms = etree.SubElement(dw_lod3, f"{{{NSMAP['gml']}}}MultiSurface", attrib={f"{{{NSMAP['gml']}}}id": dw_geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                dw_parent = dw_ms

            # Use the pre-generated surface_ids for each polygon
            for poly_coords, surface_id in zip(dw_polygons, dw_surface_ids):
                sm = etree.SubElement(dw_parent, f"{{{NSMAP['gml']}}}surfaceMember")
                poly = etree.SubElement(sm, f"{{{NSMAP['gml']}}}Polygon", attrib={f"{{{NSMAP['gml']}}}id": surface_id})
                ext = etree.SubElement(poly, f"{{{NSMAP['gml']}}}exterior")
                lr = etree.SubElement(ext, f"{{{NSMAP['gml']}}}LinearRing")
                pos = etree.SubElement(lr, f"{{{NSMAP['gml']}}}posList")
                pos.text = " ".join(f"{c:.3f}" for c in poly_coords)
        
        return materials_added

    def is_intended_solid(self, element):
        """
        Checks the IFC Representation Type to determine if the element 
        was modeled as a Solid (Volumetric) or Surface.
        """
        if not hasattr(element, "Representation") or not element.Representation:
            # Default to surface if no representation info exists
            return False
            
        # Iterate through representations (e.g., Body, Axis, Box)
        # We look for the 'Body' representation which contains the physical shape
        for rep in element.Representation.Representations:
            # Check if this representation is the 3D Body
            # (Sometimes it is None or 'Body', 'Mesh', 'Model')
            if hasattr(rep, "RepresentationIdentifier"):
                rid = rep.RepresentationIdentifier
                if rid and rid.lower() not in ['body', 'mesh', 'facetedbrep']:
                    continue
            
            # Check the Type
            if hasattr(rep, "RepresentationType"):
                rtype = rep.RepresentationType
                if rtype in SOLID_REPRESENTATION_TYPES:
                    return True
                    
        return False

    def get_geometry(self, element):
        """
        Extracts geometry. 
        Note: We no longer check watertightness here. 
        We rely on is_intended_solid(element) in the main loop.
        """
        try:
            shape = ifcopenshell.geom.create_shape(self.settings, element)
            verts = shape.geometry.verts
            faces = shape.geometry.faces
            
            raw_verts = np.array(verts).reshape(-1, 3)
            polygons = []
            
            for i in range(0, len(faces), 3):
                idx = [faces[i], faces[i+1], faces[i+2]]
                poly_coords = []
                for id in idx:
                    v = self.transform_vertex(raw_verts[id])
                    poly_coords.extend(v)
                
                v_start = self.transform_vertex(raw_verts[idx[0]])
                poly_coords.extend(v_start)
                polygons.append(poly_coords)
                
            return polygons
        except:
            return None

    def get_geometry_with_surface_ids(self, element):
        """
        Extracts geometry with unique surface IDs for each polygon.
        Also extracts per-face material information from IfcOpenShell.
        Returns a tuple: (polygons, surface_ids, face_materials) where:
        - polygons is a list of polygon coordinates
        - surface_ids is a list of UUIDs corresponding to each polygon
        - face_materials is a list of (r, g, b) tuples for each polygon (or None if no material)
        """
        try:
            shape = ifcopenshell.geom.create_shape(self.settings, element)
            geom = shape.geometry
            verts = geom.verts
            faces = geom.faces
            
            raw_verts = np.array(verts).reshape(-1, 3)
            polygons = []
            surface_ids = []
            face_materials = []
            
            # Extract material information if available (including transparency)
            materials_list = []
            if hasattr(geom, 'materials') and geom.materials:
                for mat in geom.materials:
                    color = None
                    transparency = 0.0
                    
                    # Extract diffuse color
                    if hasattr(mat, 'diffuse'):
                        diffuse = mat.diffuse
                        if hasattr(diffuse, 'r') and hasattr(diffuse, 'g') and hasattr(diffuse, 'b'):
                            try:
                                r_val = diffuse.r() if callable(diffuse.r) else diffuse.r
                                g_val = diffuse.g() if callable(diffuse.g) else diffuse.g
                                b_val = diffuse.b() if callable(diffuse.b) else diffuse.b
                                color = (r_val, g_val, b_val)
                            except:
                                pass
                        elif hasattr(diffuse, 'colour'):
                            col = diffuse.colour
                            if hasattr(col, 'r') and hasattr(col, 'g') and hasattr(col, 'b'):
                                try:
                                    r_val = col.r() if callable(col.r) else col.r
                                    g_val = col.g() if callable(col.g) else col.g
                                    b_val = col.b() if callable(col.b) else col.b
                                    color = (r_val, g_val, b_val)
                                except:
                                    pass
                        elif isinstance(diffuse, tuple) and len(diffuse) >= 3:
                            color = (diffuse[0], diffuse[1], diffuse[2])
                    
                    # Extract transparency
                    if hasattr(mat, 'transparency'):
                        try:
                            trans_val = mat.transparency() if callable(mat.transparency) else mat.transparency
                            if trans_val is not None and trans_val > 0:
                                transparency = trans_val
                        except:
                            pass
                    
                    if color:
                        materials_list.append((color[0], color[1], color[2], transparency))
            
            # Get material IDs per face
            material_ids = []
            if hasattr(geom, 'material_ids') and geom.material_ids:
                material_ids = list(geom.material_ids)
            
            for i in range(0, len(faces), 3):
                idx = [faces[i], faces[i+1], faces[i+2]]
                poly_coords = []
                for id in idx:
                    v = self.transform_vertex(raw_verts[id])
                    poly_coords.extend(v)
                
                v_start = self.transform_vertex(raw_verts[idx[0]])
                poly_coords.extend(v_start)
                polygons.append(poly_coords)
                surface_ids.append(f"UUID_{uuid.uuid4()}")
                
                # Get material for this face
                face_idx = i // 3
                if material_ids and face_idx < len(material_ids):
                    mat_id = material_ids[face_idx]
                    if mat_id < len(materials_list):
                        face_materials.append(materials_list[mat_id])
                    else:
                        face_materials.append(None)
                else:
                    face_materials.append(None)
                
            return polygons, surface_ids, face_materials
        except:
            return None, None, None

    def get_element_color(self, element):
        """
        Extracts color information from an IFC element.
        Returns a tuple (red, green, blue) with values 0.0-1.0, or None if no color is found.
        """
        try:
            # Check if element has representation with styled items
            if hasattr(element, 'Representation') and element.Representation:
                for rep in element.Representation.Representations:
                    for item in rep.Items:
                        # Check for IfcStyledItem directly
                        if item.is_a('IfcStyledItem'):
                            return self._extract_color_from_style(item)
                        # Check for StyledByItem attribute (IfcExtrudedAreaSolid, etc.)
                        if hasattr(item, 'StyledByItem') and item.StyledByItem:
                            for styled_item in item.StyledByItem:
                                if styled_item.is_a('IfcStyledItem'):
                                    return self._extract_color_from_style(styled_item)
                        # Check for mapped representations
                        if item.is_a('IfcMappedItem'):
                            mapping_source = item.MappingSource
                            if mapping_source and mapping_source.MappedRepresentation:
                                for mapped_item in mapping_source.MappedRepresentation.Items:
                                    if mapped_item.is_a('IfcStyledItem'):
                                        return self._extract_color_from_style(mapped_item)
                                    # Also check StyledByItem for mapped items
                                    if hasattr(mapped_item, 'StyledByItem') and mapped_item.StyledByItem:
                                        for styled_item in mapped_item.StyledByItem:
                                            if styled_item.is_a('IfcStyledItem'):
                                                return self._extract_color_from_style(styled_item)
            
            # Check for material associations
            if hasattr(element, 'HasAssociations') and element.HasAssociations:
                for association in element.HasAssociations:
                    if association.is_a('IfcRelAssociatesMaterial'):
                        material = association.RelatingMaterial
                        if material:
                            color = self._get_material_color(material)
                            if color:
                                return color
            
            return None
        except Exception as e:
            return None

    def _extract_color_from_style(self, styled_item):
        """Extract color from IfcStyledItem, supporting IfcPresentationStyleAssignment."""
        try:
            if hasattr(styled_item, 'Styles') and styled_item.Styles:
                for style in styled_item.Styles:
                    # Handle IfcSurfaceStyle directly
                    if style.is_a('IfcSurfaceStyle'):
                        for surface_style in style.Styles:
                            if surface_style.is_a('IfcSurfaceStyleShading'):
                                color = surface_style.SurfaceColour
                                if color:
                                    return (color.Red, color.Green, color.Blue)
                    # Handle IfcPresentationStyleAssignment (common in IFC4)
                    elif style.is_a('IfcPresentationStyleAssignment'):
                        if hasattr(style, 'Styles') and style.Styles:
                            for inner_style in style.Styles:
                                if inner_style.is_a('IfcSurfaceStyle'):
                                    for surface_style in inner_style.Styles:
                                        if surface_style.is_a('IfcSurfaceStyleShading'):
                                            color = surface_style.SurfaceColour
                                            if color:
                                                return (color.Red, color.Green, color.Blue)
            return None
        except:
            return None

    def _get_material_color(self, material):
        """Extract color from material definition."""
        try:
            if material.is_a('IfcMaterial'):
                # Check for material definition representation
                if hasattr(material, 'HasRepresentation') and material.HasRepresentation:
                    for rep in material.HasRepresentation:
                        if rep.is_a('IfcMaterialDefinitionRepresentation'):
                            for mat_rep in rep.Representations:
                                for item in mat_rep.Items:
                                    if item.is_a('IfcStyledItem'):
                                        return self._extract_color_from_style(item)
            # Handle IfcMaterialLayerSetUsage and IfcMaterialLayerSet
            elif material.is_a('IfcMaterialLayerSetUsage') or material.is_a('IfcMaterialLayerSet'):
                # Try to get color from the first layer's material
                layers = getattr(material, 'MaterialLayers', None) or getattr(material, 'ForLayerSet', None)
                if layers and len(layers) > 0:
                    layer_material = layers[0].Material
                    if layer_material:
                        return self._get_material_color(layer_material)
            return None
        except:
            return None

    def get_element_materials_with_faces(self, element):
        """
        Extracts materials/colors with their associated face indices from an IFC element.
        This supports multi-appearance by mapping different materials to different geometry parts.
        
        Returns a list of tuples: [(color, face_indices), ...] where:
        - color is a tuple (red, green, blue) with values 0.0-1.0
        - face_indices is a list of face indices this material applies to (or None for all faces)
        
        Collects ALL colors from ALL styled items, not just the first one.
        """
        try:
            materials_with_faces = []
            seen_colors = set()  # Track unique colors to avoid duplicates
            
            def add_color_if_unique(color, face_indices):
                """Add a color to materials list if not already present (avoids duplicates)."""
                color_key = (round(color[0], 6), round(color[1], 6), round(color[2], 6))
                if color_key not in seen_colors:
                    seen_colors.add(color_key)
                    materials_with_faces.append((color, face_indices))
            
            def extract_colors_from_item(item):
                """Recursively extract colors from a geometry item and its mapped representations."""
                # Check for StyledByItem (common in IFC4)
                if hasattr(item, 'StyledByItem') and item.StyledByItem:
                    for styled_item in item.StyledByItem:
                        if styled_item.is_a('IfcStyledItem'):
                            color = self._extract_color_from_style(styled_item)
                            if color:
                                add_color_if_unique(color, None)
                
                # Check for IfcMappedItem and recurse into mapped representation
                if item.is_a('IfcMappedItem'):
                    mapping_source = item.MappingSource
                    if mapping_source and mapping_source.MappedRepresentation:
                        for mapped_item in mapping_source.MappedRepresentation.Items:
                            extract_colors_from_item(mapped_item)
                
                # Check for IfcTriangulatedFaceSet with per-face colors
                if item.is_a('IfcTriangulatedFaceSet'):
                    if hasattr(item, 'HasColours') and item.HasColours:
                        colours = item.HasColours
                        if colours.is_a('IfcIndexedColourMap'):
                            color_list = colours.Colours
                            color_index = colours.ColourIndex
                            
                            if color_list and color_index:
                                color_faces = {}
                                for face_idx, color_idx in enumerate(color_index):
                                    if color_idx not in color_faces:
                                        color_faces[color_idx] = []
                                    color_faces[color_idx].append(face_idx)
                                
                                for color_idx, face_indices in color_faces.items():
                                    if hasattr(color_list, 'Colours'):
                                        color_data = color_list.Colours[color_idx - 1]
                                        if color_data:
                                            color = (color_data.Red, color_data.Green, color_data.Blue)
                                            add_color_if_unique(color, face_indices)
            
            # Check if element has representation with styled items
            if hasattr(element, 'Representation') and element.Representation:
                for rep in element.Representation.Representations:
                    if rep.RepresentationIdentifier in ['Body', 'Mesh', 'FacetedBrep']:
                        for item in rep.Items:
                            extract_colors_from_item(item)
            
            # Check for material associations with material sets
            if hasattr(element, 'HasAssociations') and element.HasAssociations:
                for association in element.HasAssociations:
                    if association.is_a('IfcRelAssociatesMaterial'):
                        material = association.RelatingMaterial
                        
                        # Handle IfcMaterialConstituentSet
                        if material and material.is_a('IfcMaterialConstituentSet'):
                            if hasattr(material, 'MaterialConstituents') and material.MaterialConstituents:
                                for constituent in material.MaterialConstituents:
                                    if hasattr(constituent, 'Material') and constituent.Material:
                                        color = self._get_material_color(constituent.Material)
                                        if color:
                                            add_color_if_unique(color, None)
                        
                        # Handle IfcMaterialLayerSetUsage
                        elif material and (material.is_a('IfcMaterialLayerSetUsage') or material.is_a('IfcMaterialLayerSet')):
                            layers = getattr(material, 'ForLayerSet', None) or material
                            if hasattr(layers, 'MaterialLayers'):
                                for layer in layers.MaterialLayers:
                                    if hasattr(layer, 'Material') and layer.Material:
                                        color = self._get_material_color(layer.Material)
                                        if color:
                                            add_color_if_unique(color, None)
                        
                        # Single material
                        elif material:
                            color = self._get_material_color(material)
                            if color:
                                add_color_if_unique(color, None)
            
            # If we found materials, return them
            if materials_with_faces:
                return materials_with_faces
            
            # Fallback: try to get a single color for the whole element
            color = self.get_element_color(element)
            if color:
                return [(color, None)]
            
            return []
        except Exception as e:
            return []

    def add_appearance(self, parent_element, element, element_id, geometry_id, surface_ids=None, face_materials=None):
        """
        Adds CityGML appearance elements if the IFC element has color information.
        Supports multi-appearance with multiple materials targeting different surfaces.
        
        Args:
            parent_element: The CityGML element to add appearance to
            element: The IFC element
            element_id: The gml:id of the CityGML element
            geometry_id: The gml:id of the geometry (Solid or MultiSurface)
            surface_ids: Optional list of surface IDs corresponding to each polygon face
            face_materials: Optional list of (r, g, b, transparency) tuples per face from IfcOpenShell
        
        Returns:
            tuple: (success: bool, material_count: int) - success flag and number of materials added
        """
        # Skip if appearances are disabled
        if getattr(self, 'no_appearances', False):
            return False, 0
        
        # If we have per-face materials from IfcOpenShell, use those (most accurate)
        if face_materials and surface_ids:
            # Group faces by material color and transparency
            material_faces = {}
            for face_idx, mat_data in enumerate(face_materials):
                if mat_data is not None:
                    # mat_data is (r, g, b, transparency) tuple
                    if len(mat_data) >= 3:
                        r, g, b = mat_data[0], mat_data[1], mat_data[2]
                        transparency = mat_data[3] if len(mat_data) > 3 else 0.0
                        # Round to avoid floating point comparison issues
                        material_key = (round(r, 6), round(g, 6), round(b, 6), round(transparency, 6))
                        if material_key not in material_faces:
                            material_faces[material_key] = []
                        material_faces[material_key].append(face_idx)
            
            if material_faces:
                try:
                    app_member = etree.SubElement(parent_element, f"{{{NSMAP['core']}}}appearance")
                    appearance = etree.SubElement(app_member, f"{{{NSMAP['app']}}}Appearance")
                    appearance.set(f"{{{NSMAP['gml']}}}id", f"APP_{element_id}")
                    
                    theme = etree.SubElement(appearance, f"{{{NSMAP['app']}}}theme")
                    theme.text = "RGB"
                    
                    material_count = 0
                    
                    for mat_idx, (material_key, face_indices) in enumerate(material_faces.items()):
                        r, g, b, transparency = material_key
                        
                        surface_data_member = etree.SubElement(appearance, f"{{{NSMAP['app']}}}surfaceData")
                        x3d_material = etree.SubElement(surface_data_member, f"{{{NSMAP['app']}}}X3DMaterial")
                        x3d_material.set(f"{{{NSMAP['gml']}}}id", f"MAT_{element_id}_{mat_idx}")
                        
                        is_front = etree.SubElement(x3d_material, f"{{{NSMAP['app']}}}isFront")
                        is_front.text = "true"
                        
                        diffuse_color = etree.SubElement(x3d_material, f"{{{NSMAP['app']}}}diffuseColor")
                        diffuse_color.text = f"{r} {g} {b}"
                        
                        # Add transparency if > 0
                        if transparency > 0:
                            trans_elem = etree.SubElement(x3d_material, f"{{{NSMAP['app']}}}transparency")
                            trans_elem.text = f"{transparency}"
                        
                        # Add targets for each face with this material
                        for face_idx in face_indices:
                            if 0 <= face_idx < len(surface_ids):
                                target = etree.SubElement(x3d_material, f"{{{NSMAP['app']}}}target")
                                target.text = f"#{surface_ids[face_idx]}"
                        
                        material_count += 1
                    
                    return True, material_count
                except Exception as e:
                    return False, 0
        
        # Fallback: use get_element_materials_with_faces (less accurate, no per-face mapping)
        materials_with_faces = self.get_element_materials_with_faces(element)
        
        if not materials_with_faces:
            return False, 0
        
        try:
            # Create appearance member
            app_member = etree.SubElement(parent_element, f"{{{NSMAP['core']}}}appearance")
            appearance = etree.SubElement(app_member, f"{{{NSMAP['app']}}}Appearance")
            appearance.set(f"{{{NSMAP['gml']}}}id", f"APP_{element_id}")
            
            # Add theme
            theme = etree.SubElement(appearance, f"{{{NSMAP['app']}}}theme")
            theme.text = "RGB"
            
            material_count = 0
            
            # Create X3DMaterial for each unique material
            for mat_idx, (color, face_indices) in enumerate(materials_with_faces):
                # Add surface data with target reference to geometry
                surface_data_member = etree.SubElement(appearance, f"{{{NSMAP['app']}}}surfaceData")
                x3d_material = etree.SubElement(surface_data_member, f"{{{NSMAP['app']}}}X3DMaterial")
                x3d_material.set(f"{{{NSMAP['gml']}}}id", f"MAT_{element_id}_{mat_idx}")
                
                # Add isFront (must come first)
                is_front = etree.SubElement(x3d_material, f"{{{NSMAP['app']}}}isFront")
                is_front.text = "true"
                
                # Add diffuse color
                diffuse_color = etree.SubElement(x3d_material, f"{{{NSMAP['app']}}}diffuseColor")
                diffuse_color.text = f"{color[0]} {color[1]} {color[2]}"
                
                # Add target reference(s) to the geometry
                if face_indices and surface_ids:
                    # Multi-appearance: target specific surfaces by their IDs
                    for face_idx in face_indices:
                        if 0 <= face_idx < len(surface_ids):
                            target = etree.SubElement(x3d_material, f"{{{NSMAP['app']}}}target")
                            target.text = f"#{surface_ids[face_idx]}"
                else:
                    # Single-appearance: target the entire geometry
                    target = etree.SubElement(x3d_material, f"{{{NSMAP['app']}}}target")
                    target.text = f"#{geometry_id}"
                
                material_count += 1
            
            return True, material_count
        except Exception as e:
            return False, 0

    def generate(self):
        """Generate CityGML 3.0 output from the IFC model and write to file."""
        root = etree.Element(f"{{{NSMAP['core']}}}CityModel", nsmap=NSMAP)
        root.set(f"{{{NSMAP['xsi']}}}schemaLocation", "http://www.opengis.net/citygml/profiles/base/3.0 http://schemas.opengis.net/citygml/profiles/base/3.0/CityGML.xsd")
        
        # cityObjectMember will be created after project metadata so
        # that project name/description become the first child elements
        
        try:
            projects = self.model.by_type("IfcProject")
            ifc_project = projects[0] if projects else None
        except RuntimeError:
            ifc_project = None

        # Add IfcProject name/description as gml elements on the CityModel element
        if ifc_project:
            proj_desc = getattr(ifc_project, 'Description', None)
            proj_name = getattr(ifc_project, 'Name', None)
            if proj_desc:
                proj_desc_el = etree.SubElement(root, f"{{{NSMAP['gml']}}}description")
                proj_desc_el.text = proj_desc
            if proj_name:
                proj_name_el = etree.SubElement(root, f"{{{NSMAP['gml']}}}name")
                proj_name_el.text = proj_name

        # Get all IFC buildings and export each as a separate CityGML Building
        try:
            ifc_buildings = self.model.by_type("IfcBuilding")
        except RuntimeError:
            ifc_buildings = []

        if not ifc_buildings:
            print("No IfcBuilding objects found in the model.")
            tree = etree.ElementTree(root)
            tree.write(self.output_path, pretty_print=True, xml_declaration=True, encoding="UTF-8")
            print(f"Successfully wrote {self.output_path}")
            return

        # Mapping of IFC types to CityGML 3.0 Building classes
        # Format: "IfcType": ("CityGML_Class", "ifc_type_for_class")
        target_elements = {
            # BuildingConstructiveElement types
            "IfcWall": ("BuildingConstructiveElement", "Wall"),
            "IfcWallStandardCase": ("BuildingConstructiveElement", "Wall"),
            "IfcRoof": ("BuildingConstructiveElement", "Roof"),
            "IfcSlab": ("BuildingConstructiveElement", "Slab"),
            "IfcColumn": ("BuildingConstructiveElement", "Column"),
            "IfcBeam": ("BuildingConstructiveElement", "Beam"),
            "IfcMember": ("BuildingConstructiveElement", "Member"),
            "IfcPlate": ("BuildingConstructiveElement", "Plate"),
            "IfcStair": ("BuildingConstructiveElement", "Stair"),
            "IfcStairFlight": ("BuildingConstructiveElement", "StairFlight"),
            "IfcRamp": ("BuildingConstructiveElement", "Ramp"),
            "IfcRampFlight": ("BuildingConstructiveElement", "RampFlight"),
            "IfcFooting": ("BuildingConstructiveElement", "Footing"),
            "IfcPile": ("BuildingConstructiveElement", "Pile"),
            "IfcBuildingElementProxy": ("BuildingConstructiveElement", "BuildingElementProxy"),
            "IfcCurtainWall": ("BuildingConstructiveElement", "CurtainWall"),
            # BuildingInstallation types
            "IfcCovering": ("BuildingInstallation", "Covering"),
            "IfcRailing": ("BuildingInstallation", "Railing"),
            # BuildingFurniture types
            "IfcFurnishingElement": ("BuildingFurniture", "FurnishingElement"),
            "IfcFurniture": ("BuildingFurniture", "Furniture"),
            "IfcSystemFurnitureElement": ("BuildingFurniture", "SystemFurnitureElement"),
            # Door and Window are handled as child elements of walls
            "IfcDoor": ("Door", "Door"),
            "IfcWindow": ("Window", "Window")
        }

        # Iterate over all IfcBuilding objects and export each one
        for ifc_bldg in ifc_buildings:
            # Track which doors and windows are embedded in constructive elements for THIS building
            embedded_doors_windows = set()
            # Reset exported elements tracking for each building
            self.exported_elements = set()
            # Dictionary to store dummy BCEs per storey (for xlinks from Storey elements)
            dummy_bce_per_storey = {}
            # Counter for appearances in this building
            building_appearance_count = 0
            # Create cityObjectMember and Building for this IfcBuilding
            member = etree.SubElement(root, f"{{{NSMAP['core']}}}cityObjectMember")
            building = etree.SubElement(member, f"{{{NSMAP['bldg']}}}Building", attrib={f"{{{NSMAP['gml']}}}id": f"UUID_{uuid.uuid4()}"})

            # Building metadata: name/description and property sets
            b_desc = getattr(ifc_bldg, 'Description', None)
            b_name = getattr(ifc_bldg, 'Name', None)
            if b_desc:
                desc_el = etree.SubElement(building, f"{{{NSMAP['gml']}}}description")
                desc_el.text = b_desc
            if b_name:
                name_el = etree.SubElement(building, f"{{{NSMAP['gml']}}}name")
                name_el.text = b_name

            # External reference using IfcBuilding.GlobalId
            ext_guid = getattr(ifc_bldg, 'GlobalId', None)
            if not ext_guid:
                ext_guid = getattr(ifc_project, 'GlobalId', "UNKNOWN") if ifc_project else "UNKNOWN"
            self.create_external_reference(building, ext_guid)

            # Add building properties
            try:
                self.add_properties(building, ifc_bldg)
            except Exception:
                pass

            # Get elements that belong to this building via decomposition
            building_elements = set(ifcopenshell.util.element.get_decomposition(ifc_bldg))

            # Get all IfcSpace objects that belong to this building
            try:
                all_spaces = self.model.by_type("IfcSpace")
            except RuntimeError:
                all_spaces = []
            rooms_list = [s for s in all_spaces if s in building_elements]

            print(f"\nConverting building: {b_name or 'Unnamed'}")

            # Collect all elements by type for this building
            # Use exact type matching (not inheritance) to avoid duplicates
            building_ifc_elements = {}
            for ifc_type, _ in target_elements.items():
                try:
                    elements = self.model.by_type(ifc_type)
                except RuntimeError:
                    elements = []
                # Filter elements to only those belonging to this building
                # AND ensure exact type match (not subtypes) to avoid duplicates
                building_ifc_elements[ifc_type] = [e for e in elements if e in building_elements and e.is_a() == ifc_type]

            # --- Process Walls with embedded Doors and Windows ---
            wall_types = ["IfcWall", "IfcWallStandardCase"]
            for wall_type in wall_types:
                walls = building_ifc_elements.get(wall_type, [])
                if walls:
                    print(f"{wall_type}: ", end="", flush=True)

                for wall in walls:
                    # Create BuildingConstructiveElement for the wall
                    cons_prop = etree.SubElement(building, f"{{{NSMAP['bldg']}}}buildingConstructiveElement")
                    gml_id = f"UUID_{uuid.uuid4()}"
                    cons_elem = etree.SubElement(cons_prop, f"{{{NSMAP['bldg']}}}BuildingConstructiveElement", attrib={f"{{{NSMAP['gml']}}}id": gml_id})
                    self.element_gml_ids[wall] = gml_id

                    # Add metadata
                    if hasattr(wall, 'Description') and wall.Description:
                        desc_elem = etree.SubElement(cons_elem, f"{{{NSMAP['gml']}}}description")
                        desc_elem.text = wall.Description
                    if hasattr(wall, 'Name') and wall.Name:
                        name_elem = etree.SubElement(cons_elem, f"{{{NSMAP['gml']}}}name")
                        name_elem.text = wall.Name

                    self.create_external_reference(cons_elem, getattr(wall, 'GlobalId', 'UNKNOWN'))
                    
                    # Generate geometry with surface IDs and per-face materials for multi-appearance support
                    is_solid = self.is_intended_solid(wall)
                    polygons, surface_ids, face_materials = self.get_geometry_with_surface_ids(wall)
                    geometry_id = f"UUID_{uuid.uuid4()}" if polygons else None
                    
                    # Add appearance if element has color information (before generic attributes)
                    # Pass surface_ids and face_materials for per-face targeting
                    if geometry_id:
                        success, mat_count = self.add_appearance(cons_elem, wall, gml_id, geometry_id, surface_ids, face_materials)
                        if success:
                            building_appearance_count += mat_count
                    
                    # Add generic attributes (after appearance)
                    self.add_properties(cons_elem, wall)
                    
                    # Output geometry (after generic attributes)
                    if polygons:
                        if is_solid:
                            lod3 = etree.SubElement(cons_elem, f"{{{NSMAP['core']}}}lod3Solid")
                            solid = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}Solid", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            exterior = etree.SubElement(solid, f"{{{NSMAP['gml']}}}exterior")
                            shell = etree.SubElement(exterior, f"{{{NSMAP['gml']}}}Shell")
                            parent_for_polys = shell
                        else:
                            lod3 = etree.SubElement(cons_elem, f"{{{NSMAP['core']}}}lod3MultiSurface")
                            ms = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}MultiSurface", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            parent_for_polys = ms

                        # Use the pre-generated surface_ids for each polygon
                        for poly_coords, surface_id in zip(polygons, surface_ids):
                            sm = etree.SubElement(parent_for_polys, f"{{{NSMAP['gml']}}}surfaceMember")
                            poly = etree.SubElement(sm, f"{{{NSMAP['gml']}}}Polygon", attrib={f"{{{NSMAP['gml']}}}id": surface_id})
                            ext = etree.SubElement(poly, f"{{{NSMAP['gml']}}}exterior")
                            lr = etree.SubElement(ext, f"{{{NSMAP['gml']}}}LinearRing")
                            pos = etree.SubElement(lr, f"{{{NSMAP['gml']}}}posList")
                            pos.text = " ".join(f"{c:.3f}" for c in poly_coords)

                        # Mark wall as successfully exported
                        self.exported_elements.add(wall)

                    # Find and add doors/windows as child elements using con:filling
                    # (must come before bldg:class for schema validation)
                    doors_windows = self.get_doors_and_windows_in_element(wall)
                    for dw in doors_windows:
                        embedded_doors_windows.add(dw)
                        mat_added = self._add_door_or_window_as_filling(cons_elem, dw, building_appearance_count)
                        building_appearance_count += mat_added
                        # Output D for Door or W for Window
                        if dw.is_a("IfcDoor"):
                            print("D", end="", flush=True)
                        else:
                            print("W", end="", flush=True)

                    # Add class for wall (must come after con:filling)
                    class_elem = etree.SubElement(cons_elem, f"{{{NSMAP['bldg']}}}class")
                    class_elem.text = wall_type

                    print(".", end="", flush=True)

                if walls:
                    print()

            # --- Process remaining BuildingConstructiveElement types (excluding walls) ---
            constructive_types = [
                "IfcRoof", "IfcSlab", "IfcColumn", "IfcBeam", "IfcMember", "IfcPlate",
                "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight",
                "IfcFooting", "IfcPile", "IfcBuildingElementProxy", "IfcCurtainWall"
            ]

            for ifc_type in constructive_types:
                elements = building_ifc_elements.get(ifc_type, [])
                if elements:
                    print(f"{ifc_type}: ", end="", flush=True)

                for elem in elements:
                    cons_prop = etree.SubElement(building, f"{{{NSMAP['bldg']}}}buildingConstructiveElement")
                    gml_id = f"UUID_{uuid.uuid4()}"
                    cons_elem = etree.SubElement(cons_prop, f"{{{NSMAP['bldg']}}}BuildingConstructiveElement", attrib={f"{{{NSMAP['gml']}}}id": gml_id})
                    self.element_gml_ids[elem] = gml_id

                    if hasattr(elem, 'Description') and elem.Description:
                        desc_elem = etree.SubElement(cons_elem, f"{{{NSMAP['gml']}}}description")
                        desc_elem.text = elem.Description
                    if hasattr(elem, 'Name') and elem.Name:
                        name_elem = etree.SubElement(cons_elem, f"{{{NSMAP['gml']}}}name")
                        name_elem.text = elem.Name

                    self.create_external_reference(cons_elem, getattr(elem, 'GlobalId', 'UNKNOWN'))
                    
                    # Generate geometry UUID early (needed for appearance)
                    is_solid = self.is_intended_solid(elem)
                    # Generate geometry with surface IDs for multi-appearance support
                    polygons, surface_ids, face_materials = self.get_geometry_with_surface_ids(elem)
                    geometry_id = f"UUID_{uuid.uuid4()}" if polygons else None
                    
                    # Add appearance if element has color information (before generic attributes)
                    # Pass surface_ids to enable multi-appearance targeting
                    if geometry_id:
                        success, mat_count = self.add_appearance(cons_elem, elem, gml_id, geometry_id, surface_ids, face_materials)
                        if success:
                            building_appearance_count += mat_count
                    
                    # Add generic attributes (after appearance)
                    self.add_properties(cons_elem, elem)
                    
                    # Output geometry (after generic attributes)
                    if polygons:
                        if is_solid:
                            lod3 = etree.SubElement(cons_elem, f"{{{NSMAP['core']}}}lod3Solid")
                            solid = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}Solid", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            exterior = etree.SubElement(solid, f"{{{NSMAP['gml']}}}exterior")
                            shell = etree.SubElement(exterior, f"{{{NSMAP['gml']}}}Shell")
                            parent_for_polys = shell
                        else:
                            lod3 = etree.SubElement(cons_elem, f"{{{NSMAP['core']}}}lod3MultiSurface")
                            ms = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}MultiSurface", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            parent_for_polys = ms

                        # Use the pre-generated surface_ids for each polygon
                        for poly_coords, surface_id in zip(polygons, surface_ids):
                            sm = etree.SubElement(parent_for_polys, f"{{{NSMAP['gml']}}}surfaceMember")
                            poly = etree.SubElement(sm, f"{{{NSMAP['gml']}}}Polygon", attrib={f"{{{NSMAP['gml']}}}id": surface_id})
                            ext = etree.SubElement(poly, f"{{{NSMAP['gml']}}}exterior")
                            lr = etree.SubElement(ext, f"{{{NSMAP['gml']}}}LinearRing")
                            pos = etree.SubElement(lr, f"{{{NSMAP['gml']}}}posList")
                            pos.text = " ".join(f"{c:.3f}" for c in poly_coords)

                        print(".", end="", flush=True)
                        # Mark element as successfully exported
                        self.exported_elements.add(elem)
                    else:
                        building.remove(cons_prop)

                    # Find and add doors/windows as child elements using con:filling
                    # (must come before bldg:class for schema validation)
                    doors_windows = self.get_doors_and_windows_in_element(elem)
                    for dw in doors_windows:
                        embedded_doors_windows.add(dw)
                        mat_added = self._add_door_or_window_as_filling(cons_elem, dw, building_appearance_count)
                        building_appearance_count += mat_added
                        # Output D for Door or W for Window
                        if dw.is_a("IfcDoor"):
                            print("D", end="", flush=True)
                        else:
                            print("W", end="", flush=True)

                    # Add class element (must come after con:filling)
                    class_elem = etree.SubElement(cons_elem, f"{{{NSMAP['bldg']}}}class")
                    class_elem.text = ifc_type

                if elements:
                    print()

            # --- Check for non-exported doors and windows for THIS building ---
            # Get all doors and windows that belong to this building
            building_doors = [e for e in self.model.by_type("IfcDoor") if e in building_elements]
            building_windows = [e for e in self.model.by_type("IfcWindow") if e in building_elements]
            building_doors_windows = building_doors + building_windows
            
            total_doors_windows = len(building_doors_windows)
            exported_count = len(embedded_doors_windows)
            unmapped_count = total_doors_windows - exported_count
            
            # Track unmapped doors/windows without storey for fallback dummy BCE
            unmapped_without_storey = []
            
            if total_doors_windows > 0:
                print(f"\nDoors and Windows: {exported_count} of {total_doors_windows} exported as con:filling")
                if unmapped_count > 0:
                    print(f"  Warning: {unmapped_count} doors/windows could not be assigned to a BuildingConstructiveElement")
                    if not getattr(self, 'list_unmapped_doors_windows', False) and not getattr(self, 'unrelated_doors_windows_in_dummy_bce', False):                      
                        print("  Use option '--list-unmapped-doors-and-windows' to see details, or")
                        print("  use option '--unrelated-doors-and-windows-in-dummy-bce' to create empty")
                        print("  BuildingConstructiveElements grouped by storey.")

                    if getattr(self, 'list_unmapped_doors_windows', False):
                        # List the unmapped doors and windows for this building
                        unmapped = [dw for dw in building_doors_windows if dw not in embedded_doors_windows]
                        self._list_unmapped_doors_windows(unmapped)
                    
                    # If option is set, create dummy BuildingConstructiveElements for unmapped doors/windows
                    if getattr(self, 'unrelated_doors_windows_in_dummy_bce', False):
                        unmapped = [dw for dw in building_doors_windows if dw not in embedded_doors_windows]
                        if unmapped:
                            print("\nCreating dummy BuildingConstructiveElements for unrelated doors/windows...")
                            
                            # Group unmapped doors/windows by storey
                            doors_windows_by_storey = {}
                            for dw in unmapped:
                                # Find which storey this door/window belongs to
                                storey = self._find_storey_for_element(dw)
                                if storey:
                                    storey_guid = getattr(storey, 'GlobalId', None)
                                    if storey_guid:
                                        if storey_guid not in doors_windows_by_storey:
                                            doors_windows_by_storey[storey_guid] = {'storey': storey, 'elements': []}
                                        doors_windows_by_storey[storey_guid]['elements'].append(dw)
                                else:
                                    unmapped_without_storey.append(dw)
                            
                            # Create dummy BCE for each storey with unmapped elements
                            for storey_guid, data in doors_windows_by_storey.items():
                                storey = data['storey']
                                elements = data['elements']
                                storey_name = getattr(storey, 'Name', 'Unnamed Storey')
                                
                                print(f"  Creating dummy BCE for storey '{storey_name}': ", end="", flush=True)
                                dummy_prop = etree.SubElement(building, f"{{{NSMAP['bldg']}}}buildingConstructiveElement")
                                dummy_gml_id = f"UUID_{uuid.uuid4()}"
                                dummy_elem = etree.SubElement(dummy_prop, f"{{{NSMAP['bldg']}}}BuildingConstructiveElement", attrib={f"{{{NSMAP['gml']}}}id": dummy_gml_id})
                                
                                # Store reference for later use by Storey element
                                dummy_bce_per_storey[storey_guid] = dummy_gml_id
                                
                                # Add name indicating which storey this belongs to
                                name_elem = etree.SubElement(dummy_elem, f"{{{NSMAP['gml']}}}name")
                                name_elem.text = f"Stub Element for unrelated Doors and Windows - Storey: {storey_name}"
                                
                                # Add all doors/windows for this storey as fillings
                                for dw in elements:
                                    mat_added = self._add_door_or_window_as_filling(dummy_elem, dw, building_appearance_count)
                                    building_appearance_count += mat_added
                                    # Output D for Door or W for Window
                                    if dw.is_a("IfcDoor"):
                                        print("D", end="", flush=True)
                                    else:
                                        print("W", end="", flush=True)
                                
                                # Add class (must come after con:filling for schema validation)
                                class_elem = etree.SubElement(dummy_elem, f"{{{NSMAP['bldg']}}}class")
                                class_elem.text = "DummyBuildingConstructiveElement"
                                print()
                            
                            # Create fallback dummy BCE for elements not associated with any storey
                            if unmapped_without_storey:
                                print(f"  Creating fallback dummy BCE for elements without storey: ", end="", flush=True)
                                dummy_prop = etree.SubElement(building, f"{{{NSMAP['bldg']}}}buildingConstructiveElement")
                                dummy_gml_id = f"UUID_{uuid.uuid4()}"
                                dummy_elem = etree.SubElement(dummy_prop, f"{{{NSMAP['bldg']}}}BuildingConstructiveElement", attrib={f"{{{NSMAP['gml']}}}id": dummy_gml_id})
                                
                                # Store reference with special key for unmapped elements
                                dummy_bce_per_storey['__UNMAPPED__'] = dummy_gml_id
                                
                                # Add name indicating no storey association
                                name_elem = etree.SubElement(dummy_elem, f"{{{NSMAP['gml']}}}name")
                                name_elem.text = "Stub Element for unrelated Doors and Windows - No Storey Assignment"
                                
                                # Add all unmapped doors/windows as fillings
                                for dw in unmapped_without_storey:
                                    mat_added = self._add_door_or_window_as_filling(dummy_elem, dw, building_appearance_count)
                                    building_appearance_count += mat_added
                                    # Output D for Door or W for Window
                                    if dw.is_a("IfcDoor"):
                                        print("D", end="", flush=True)
                                    else:
                                        print("W", end="", flush=True)
                                
                                # Add class (must come after con:filling for schema validation)
                                class_elem = etree.SubElement(dummy_elem, f"{{{NSMAP['bldg']}}}class")
                                class_elem.text = "DummyBuildingConstructiveElement"
                                print()
                print()

            # --- Process BuildingInstallation types ---
            installation_types = ["IfcCovering", "IfcRailing"]

            for ifc_type in installation_types:
                elements = building_ifc_elements.get(ifc_type, [])
                if elements:
                    print(f"{ifc_type}: ", end="", flush=True)

                for elem in elements:
                    inst_prop = etree.SubElement(building, f"{{{NSMAP['bldg']}}}buildingInstallation")
                    gml_id = f"UUID_{uuid.uuid4()}"
                    inst_elem = etree.SubElement(inst_prop, f"{{{NSMAP['bldg']}}}BuildingInstallation", attrib={f"{{{NSMAP['gml']}}}id": gml_id})
                    self.element_gml_ids[elem] = gml_id

                    if hasattr(elem, 'Description') and elem.Description:
                        desc_elem = etree.SubElement(inst_elem, f"{{{NSMAP['gml']}}}description")
                        desc_elem.text = elem.Description
                    if hasattr(elem, 'Name') and elem.Name:
                        name_elem = etree.SubElement(inst_elem, f"{{{NSMAP['gml']}}}name")
                        name_elem.text = elem.Name

                    self.create_external_reference(inst_elem, getattr(elem, 'GlobalId', 'UNKNOWN'))
                    
                    # Generate geometry with surface IDs for multi-appearance support
                    is_solid = self.is_intended_solid(elem)
                    polygons, surface_ids, face_materials = self.get_geometry_with_surface_ids(elem)
                    geometry_id = f"UUID_{uuid.uuid4()}" if polygons else None
                    
                    # Add appearance if element has color information (before generic attributes)
                    # Pass surface_ids to enable multi-appearance targeting
                    if geometry_id:
                        success, mat_count = self.add_appearance(inst_elem, elem, gml_id, geometry_id, surface_ids, face_materials)
                        if success:
                            building_appearance_count += mat_count
                    
                    # Add generic attributes (after appearance)
                    self.add_properties(inst_elem, elem)
                    
                    # Output geometry (after generic attributes)
                    if polygons:
                        if is_solid:
                            lod3 = etree.SubElement(inst_elem, f"{{{NSMAP['core']}}}lod3Solid")
                            solid = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}Solid", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            exterior = etree.SubElement(solid, f"{{{NSMAP['gml']}}}exterior")
                            shell = etree.SubElement(exterior, f"{{{NSMAP['gml']}}}Shell")
                            parent_for_polys = shell
                        else:
                            lod3 = etree.SubElement(inst_elem, f"{{{NSMAP['core']}}}lod3MultiSurface")
                            ms = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}MultiSurface", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            parent_for_polys = ms

                        # Use the pre-generated surface_ids for each polygon
                        for poly_coords, surface_id in zip(polygons, surface_ids):
                            sm = etree.SubElement(parent_for_polys, f"{{{NSMAP['gml']}}}surfaceMember")
                            poly = etree.SubElement(sm, f"{{{NSMAP['gml']}}}Polygon", attrib={f"{{{NSMAP['gml']}}}id": surface_id})
                            ext = etree.SubElement(poly, f"{{{NSMAP['gml']}}}exterior")
                            lr = etree.SubElement(ext, f"{{{NSMAP['gml']}}}LinearRing")
                            pos = etree.SubElement(lr, f"{{{NSMAP['gml']}}}posList")
                            pos.text = " ".join(f"{c:.3f}" for c in poly_coords)

                        print(".", end="", flush=True)
                        # Mark element as successfully exported
                        self.exported_elements.add(elem)
                    else:
                        building.remove(inst_prop)

                    class_elem = etree.SubElement(inst_elem, f"{{{NSMAP['bldg']}}}class")
                    class_elem.text = ifc_type

                if elements:
                    print()

            # Export IfcSpace rooms BEFORE BuildingFurniture for schema validation
            # CityGML 3.0 requires: buildingConstructiveElement -> buildingInstallation -> buildingRoom -> buildingFurniture -> buildingSubdivision
            if rooms_list:
                print("IfcSpace: ", end="", flush=True)
                for elem in rooms_list:
                    room_prop = etree.SubElement(building, f"{{{NSMAP['bldg']}}}buildingRoom")
                    gml_id = f"UUID_{uuid.uuid4()}"
                    room_elem = etree.SubElement(room_prop, f"{{{NSMAP['bldg']}}}BuildingRoom", attrib={f"{{{NSMAP['gml']}}}id": gml_id})
                    # Track the gml:id for this room so we can create xlinks from Storeys
                    self.element_gml_ids[elem] = gml_id

                    r_desc = getattr(elem, 'Description', None)
                    r_name = getattr(elem, 'Name', None)
                    if r_desc:
                        desc_el = etree.SubElement(room_elem, f"{{{NSMAP['gml']}}}description")
                        desc_el.text = r_desc
                    if r_name:
                        name_el = etree.SubElement(room_elem, f"{{{NSMAP['gml']}}}name")
                        name_el.text = r_name

                    self.create_external_reference(room_elem, getattr(elem, 'GlobalId', 'UNKNOWN'))
                    
                    # Generate geometry with surface IDs for multi-appearance support
                    is_solid = self.is_intended_solid(elem)
                    polygons, surface_ids, face_materials = self.get_geometry_with_surface_ids(elem)
                    geometry_id = f"UUID_{uuid.uuid4()}" if polygons else None
                    
                    # Add appearance if element has color information (before generic attributes)
                    # Pass surface_ids to enable multi-appearance targeting
                    if geometry_id:
                        success, mat_count = self.add_appearance(room_elem, elem, gml_id, geometry_id, surface_ids, face_materials)
                        if success:
                            building_appearance_count += mat_count
                    
                    # Add generic attributes (after appearance)
                    self.add_properties(room_elem, elem)
                    
                    # Output geometry (after generic attributes)
                    if polygons:
                        if is_solid:
                            lod3 = etree.SubElement(room_elem, f"{{{NSMAP['core']}}}lod3Solid")
                            solid = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}Solid", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            exterior = etree.SubElement(solid, f"{{{NSMAP['gml']}}}exterior")
                            shell = etree.SubElement(exterior, f"{{{NSMAP['gml']}}}Shell")
                            parent_for_polys = shell
                        else:
                            lod3 = etree.SubElement(room_elem, f"{{{NSMAP['core']}}}lod3MultiSurface")
                            ms = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}MultiSurface", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            parent_for_polys = ms

                        # Use the pre-generated surface_ids for each polygon
                        for poly_coords, surface_id in zip(polygons, surface_ids):
                            sm = etree.SubElement(parent_for_polys, f"{{{NSMAP['gml']}}}surfaceMember")
                            poly = etree.SubElement(sm, f"{{{NSMAP['gml']}}}Polygon", attrib={f"{{{NSMAP['gml']}}}id": surface_id})
                            ext = etree.SubElement(poly, f"{{{NSMAP['gml']}}}exterior")
                            lr = etree.SubElement(ext, f"{{{NSMAP['gml']}}}LinearRing")
                            pos = etree.SubElement(lr, f"{{{NSMAP['gml']}}}posList")
                            pos.text = " ".join(f"{c:.3f}" for c in poly_coords)

                        print(".", end="", flush=True)
                        # Mark element as successfully exported
                        self.exported_elements.add(elem)
                    else:
                        building.remove(room_prop)

                    class_elem = etree.SubElement(room_elem, f"{{{NSMAP['bldg']}}}class")
                    class_elem.text = 'IfcSpace'
                print()

            # --- Process BuildingFurniture types (after BuildingRoom for schema validation) ---
            furniture_types = ["IfcFurniture", "IfcSystemFurnitureElement", "IfcFurnishingElement"]

            for ifc_type in furniture_types:
                elements = building_ifc_elements.get(ifc_type, [])
                if elements:
                    print(f"{ifc_type}: ", end="", flush=True)

                for elem in elements:
                    furn_prop = etree.SubElement(building, f"{{{NSMAP['bldg']}}}buildingFurniture")
                    gml_id = f"UUID_{uuid.uuid4()}"
                    furn_elem = etree.SubElement(furn_prop, f"{{{NSMAP['bldg']}}}BuildingFurniture", attrib={f"{{{NSMAP['gml']}}}id": gml_id})
                    self.element_gml_ids[elem] = gml_id

                    if hasattr(elem, 'Description') and elem.Description:
                        desc_elem = etree.SubElement(furn_elem, f"{{{NSMAP['gml']}}}description")
                        desc_elem.text = elem.Description
                    if hasattr(elem, 'Name') and elem.Name:
                        name_elem = etree.SubElement(furn_elem, f"{{{NSMAP['gml']}}}name")
                        name_elem.text = elem.Name

                    self.create_external_reference(furn_elem, getattr(elem, 'GlobalId', 'UNKNOWN'))
                    
                    # Generate geometry with surface IDs for multi-appearance support
                    is_solid = self.is_intended_solid(elem)
                    polygons, surface_ids, face_materials = self.get_geometry_with_surface_ids(elem)
                    geometry_id = f"UUID_{uuid.uuid4()}" if polygons else None
                    
                    # Add appearance if element has color information (before generic attributes)
                    # Pass surface_ids to enable multi-appearance targeting
                    if geometry_id:
                        success, mat_count = self.add_appearance(furn_elem, elem, gml_id, geometry_id, surface_ids, face_materials)
                        if success:
                            building_appearance_count += mat_count
                    
                    # Add generic attributes (after appearance)
                    self.add_properties(furn_elem, elem)
                    
                    # Output geometry (after generic attributes)
                    if polygons:
                        if is_solid:
                            lod3 = etree.SubElement(furn_elem, f"{{{NSMAP['core']}}}lod3Solid")
                            solid = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}Solid", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            exterior = etree.SubElement(solid, f"{{{NSMAP['gml']}}}exterior")
                            shell = etree.SubElement(exterior, f"{{{NSMAP['gml']}}}Shell")
                            parent_for_polys = shell
                        else:
                            lod3 = etree.SubElement(furn_elem, f"{{{NSMAP['core']}}}lod3MultiSurface")
                            ms = etree.SubElement(lod3, f"{{{NSMAP['gml']}}}MultiSurface", attrib={f"{{{NSMAP['gml']}}}id": geometry_id, "srsName": self.srs_name, "srsDimension": "3"})
                            parent_for_polys = ms

                        # Use the pre-generated surface_ids for each polygon
                        for poly_coords, surface_id in zip(polygons, surface_ids):
                            sm = etree.SubElement(parent_for_polys, f"{{{NSMAP['gml']}}}surfaceMember")
                            poly = etree.SubElement(sm, f"{{{NSMAP['gml']}}}Polygon", attrib={f"{{{NSMAP['gml']}}}id": surface_id})
                            ext = etree.SubElement(poly, f"{{{NSMAP['gml']}}}exterior")
                            lr = etree.SubElement(ext, f"{{{NSMAP['gml']}}}LinearRing")
                            pos = etree.SubElement(lr, f"{{{NSMAP['gml']}}}posList")
                            pos.text = " ".join(f"{c:.3f}" for c in poly_coords)

                        print(".", end="", flush=True)
                        # Mark element as successfully exported
                        self.exported_elements.add(elem)
                    else:
                        building.remove(furn_prop)

                    class_elem = etree.SubElement(furn_elem, f"{{{NSMAP['bldg']}}}class")
                    class_elem.text = ifc_type

                if elements:
                    print()

            # Export IfcBuildingStorey features with xlinks to rooms and constructive elements
            # Skip if --no-storeys option is set
            if not getattr(self, 'no_storeys', False):
                try:
                    all_storeys = self.model.by_type("IfcBuildingStorey")
                except RuntimeError:
                    all_storeys = []
                
                storeys_list = [s for s in all_storeys if s in building_elements]
                
                if storeys_list:
                    print("IfcBuildingStorey: ", end="", flush=True)
                    for storey in storeys_list:
                        storey_prop = etree.SubElement(building, f"{{{NSMAP['bldg']}}}buildingSubdivision")
                        storey_elem = etree.SubElement(storey_prop, f"{{{NSMAP['bldg']}}}Storey", attrib={f"{{{NSMAP['gml']}}}id": f"UUID_{uuid.uuid4()}"})
                        
                        # Add metadata
                        s_desc = getattr(storey, 'Description', None)
                        s_name = getattr(storey, 'Name', None)
                        if s_desc:
                            desc_el = etree.SubElement(storey_elem, f"{{{NSMAP['gml']}}}description")
                            desc_el.text = s_desc
                        if s_name:
                            name_el = etree.SubElement(storey_elem, f"{{{NSMAP['gml']}}}name")
                            name_el.text = s_name
                        
                        # Add external reference
                        self.create_external_reference(storey_elem, getattr(storey, 'GlobalId', 'UNKNOWN'))
                        
                        # Add properties
                        self.add_properties(storey_elem, storey)
                        
                        # Get all elements that belong to this storey
                        # Use both decomposition and ContainedInStructure relations
                        storey_elements = self._get_storey_elements(storey)
                        
                        # Create xlinks to constructive elements that belong to this storey
                        # (Doors and Windows are not linked separately as they are embedded in walls)
                        all_element_types = [
                            "IfcWall", "IfcWallStandardCase", "IfcRoof", "IfcSlab", "IfcColumn", "IfcBeam",
                            "IfcMember", "IfcPlate", "IfcStair", "IfcStairFlight", "IfcRamp", "IfcRampFlight",
                            "IfcFooting", "IfcPile", "IfcBuildingElementProxy",
                            "IfcCurtainWall", "IfcCovering", "IfcRailing",
                            "IfcFurniture", "IfcSystemFurnitureElement", "IfcFurnishingElement"
                        ]
                        
                        for elem_type in all_element_types:
                            try:
                                type_elements = self.model.by_type(elem_type)
                            except RuntimeError:
                                type_elements = []
                            
                            type_elements = [e for e in type_elements if e in building_elements and e in storey_elements]
                            
                            for elem in type_elements:
                                # Only create xlink if element was actually exported (has geometry)
                                if elem in self.element_gml_ids and elem in self.exported_elements:
                                    elem_gml_id = self.element_gml_ids[elem]
                                    contains = etree.SubElement(storey_elem, f"{{{NSMAP['bldg']}}}buildingConstructiveElement")
                                    contains.set(f"{{{NSMAP['xlink']}}}href", f"#{elem_gml_id}")
                        
                        # Create xlink to dummy BuildingConstructiveElement if this storey has one
                        # (must come after regular BuildingConstructiveElements but before BuildingRooms)
                        if 'dummy_bce_per_storey' in locals():
                            storey_guid = getattr(storey, 'GlobalId', None)
                            if storey_guid and storey_guid in dummy_bce_per_storey:
                                dummy_gml_id = dummy_bce_per_storey[storey_guid]
                                contains = etree.SubElement(storey_elem, f"{{{NSMAP['bldg']}}}buildingConstructiveElement")
                                contains.set(f"{{{NSMAP['xlink']}}}href", f"#{dummy_gml_id}")
                        
                        # Create xlinks to rooms that belong to this storey
                        for room in rooms_list:
                            if room in storey_elements:
                                # Only create xlink if room was actually exported (has geometry)
                                if room in self.element_gml_ids and room in self.exported_elements:
                                    room_gml_id = self.element_gml_ids[room]
                                    contains = etree.SubElement(storey_elem, f"{{{NSMAP['bldg']}}}buildingRoom")
                                    contains.set(f"{{{NSMAP['xlink']}}}href", f"#{room_gml_id}")
                        
                        print(".", end="", flush=True)
                    print()
                
                # Print appearance count for this building
                if building_appearance_count > 0:
                    print(f"Total materials/appearances in this building: {building_appearance_count}")
                
        tree = etree.ElementTree(root)
        tree.write(self.output_path, pretty_print=True, xml_declaration=True, encoding="UTF-8")
        print(f"Successfully wrote {self.output_path}")
        # If georeference override was requested, print the exact coordinates used
        if getattr(self, 'georef_oktoberfest', False):
            print(f"Georeference used (EPSG:25832): Easting={self.eastings:.3f}, Northing={self.northings:.3f}, Height={self.orthogonal_height:.3f}")
        # Print offset information if any offset was applied
        if self.xoffset != 0.0 or self.yoffset != 0.0 or self.zoffset != 0.0:
            print(f"Offset applied: X={self.xoffset:.3f}, Y={self.yoffset:.3f}, Z={self.zoffset:.3f}")

    def _list_unmapped_doors_windows(self, unmapped_doors_windows):
        """
        Lists all doors and windows that could not be mapped to a BuildingConstructiveElement
        for the current building.
        """
        if not unmapped_doors_windows:
            return
        
        print("\n  Unmapped Doors and Windows:")
        print("  " + "-"*76)
        for dw in unmapped_doors_windows:
            dw_class = dw.is_a()
            dw_guid = getattr(dw, 'GlobalId', 'N/A')
            dw_name = getattr(dw, 'Name', 'N/A')
            
            print(f"  {dw_class} | GUID: {dw_guid} | Name: {dw_name}")
            print("    Connected to:")
            
            # Find connected elements via IfcOpeningElement
            if hasattr(dw, 'FillsVoids') and dw.FillsVoids:
                for rel_fills in dw.FillsVoids:
                    if rel_fills.is_a('IfcRelFillsElement'):
                        opening = rel_fills.RelatingOpeningElement
                        if opening and opening.is_a('IfcOpeningElement'):
                            # Find the element that has this opening
                            if hasattr(opening, 'VoidsElements') and opening.VoidsElements:
                                for rel_voids in opening.VoidsElements:
                                    if rel_voids.is_a('IfcRelVoidsElement'):
                                        host_elem = rel_voids.RelatingBuildingElement
                                        if host_elem:
                                            host_class = host_elem.is_a()
                                            host_guid = getattr(host_elem, 'GlobalId', 'N/A')
                                            host_name = getattr(host_elem, 'Name', 'N/A')
                                            print(f"      - {host_class} | GUID: {host_guid} | Name: {host_name}")
        print("  " + "-"*76)

    def _find_storey_for_element(self, element):
        """
        Finds the IfcBuildingStorey that contains the given element.
        Returns the storey object or None if not found.
        """
        # Check if element has ContainedInStructure relation
        if hasattr(element, 'ContainedInStructure') and element.ContainedInStructure:
            for rel in element.ContainedInStructure:
                if rel.is_a('IfcRelContainedInSpatialStructure'):
                    relating_structure = rel.RelatingStructure
                    if relating_structure and relating_structure.is_a('IfcBuildingStorey'):
                        return relating_structure
                    # Check if it's in a building (and recursively find storey)
                    if relating_structure and relating_structure.is_a('IfcBuilding'):
                        # Try to find through Decomposes relation
                        if hasattr(element, 'Decomposes') and element.Decomposes:
                            for decomp in element.Decomposes:
                                if decomp.is_a('IfcRelAggregates') or decomp.is_a('IfcRelNests'):
                                    if decomp.RelatingObject and decomp.RelatingObject.is_a('IfcBuildingStorey'):
                                        return decomp.RelatingObject
        
        # Alternative: Check Decomposes relation directly
        if hasattr(element, 'Decomposes') and element.Decomposes:
            for decomp in element.Decomposes:
                if decomp.is_a('IfcRelAggregates') or decomp.is_a('IfcRelNests'):
                    relating_obj = decomp.RelatingObject
                    if relating_obj and relating_obj.is_a('IfcBuildingStorey'):
                        return relating_obj
                    # If it's in another element, check that element's storey
                    if relating_obj:
                        return self._find_storey_for_element(relating_obj)
        
        return None

    def _get_storey_elements(self, storey):
        """
        Gets all elements that belong to a storey, including those connected via
        ContainedInStructure (IfcRelContainedInSpatialStructure) relation.
        """
        storey_elements = set()
        
        # Get elements via decomposition (Decomposes relation)
        storey_elements.update(ifcopenshell.util.element.get_decomposition(storey))
        
        # Get elements via ContainsElements (ContainedInStructure relation)
        if hasattr(storey, 'ContainsElements') and storey.ContainsElements:
            for rel in storey.ContainsElements:
                if rel.is_a('IfcRelContainedInSpatialStructure'):
                    storey_elements.update(rel.RelatedElements)
        
        return storey_elements

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert an IFC file to CityGML 3.0")
    parser.add_argument("input_ifc", help="Path to input IFC")
    parser.add_argument("-o", "--output", help="Output path")
    parser.add_argument("--no-references", action="store_true", help="Do not export CityGML external references")
    parser.add_argument("--no-properties", action="store_true", help="Do not export property sets/generic attributes")
    parser.add_argument("--reorient-shells", action="store_true", help="Ensure that all solid boundary surfaces are oriented outwards (slows down processing!)")
    parser.add_argument("--georef-oktoberfest", action="store_true", help="Georeference model to Theresienwiese in Munich (EPSG:25832) to enable viewing in a GIS")
    parser.add_argument("--list-unmapped-doors-and-windows", action="store_true", help="List all doors and windows that could not be assigned to a BuildingConstructiveElement")
    parser.add_argument("--unrelated-doors-and-windows-in-dummy-bce", action="store_true", help="Put unrelated doors and windows in a dummy BuildingConstructiveElement")
    parser.add_argument("--no-generic-attribute-sets", action="store_true", help="Output IFC properties as direct generic attributes instead of wrapped in GenericAttributeSets")
    parser.add_argument("--pset-names-as-prefixes", action="store_true", help="Prefix property names with their property set name (e.g., [PSET_NAME]property_name)")
    parser.add_argument("--no-storeys", action="store_true", help="Do not export CityGML Storey objects")
    parser.add_argument("--no-appearances", action="store_true", help="Do not export CityGML appearance elements (colors/materials)")
    parser.add_argument("--xoffset", type=float, default=0.0, help="Offset to shift the model in X direction (applied after georeferencing)")
    parser.add_argument("--yoffset", type=float, default=0.0, help="Offset to shift the model in Y direction (applied after georeferencing)")
    parser.add_argument("--zoffset", type=float, default=0.0, help="Offset to shift the model in Z direction (applied after georeferencing)")
    args = parser.parse_args()

    input_path = args.input_ifc
    output_path = args.output if args.output else os.path.splitext(input_path)[0] + ".gml"

    converter = CityGMLGenerator(input_path, output_path, no_references=args.no_references, reorient_shells=args.reorient_shells, no_properties=args.no_properties, georef_oktoberfest=args.georef_oktoberfest, list_unmapped_doors_windows=args.list_unmapped_doors_and_windows, unrelated_doors_windows_in_dummy_bce=args.unrelated_doors_and_windows_in_dummy_bce, no_generic_attribute_sets=args.no_generic_attribute_sets, pset_names_as_prefixes=args.pset_names_as_prefixes, no_storeys=args.no_storeys, no_appearances=args.no_appearances, xoffset=args.xoffset, yoffset=args.yoffset, zoffset=args.zoffset)
    converter.generate()
    
