# ifc-to-citygml3
An FME workspace for converting IFC data sets to CityGML 3.0 data sets.


## FME workspace
The FME workspace was originally created using FME 2019.0 (Build 19238), but was last executed using FME 2020.2 (Build 20806). </br>
Opening the workspace with other FME versions might lead to errors. 

The FME workspace makes use of the generic GML Writer to create the CityGML 3.0 data sets.

The CityGML 3.0 XML schemata required by the FME workspace are provided in the 'xsds' folder. </br>
The XML schemata are equivalent to release 3.0.0-draft.2020.09.17.1 available in the [OGC CityGML 3.0 Encodings GitHub repository](https://github.com/opengeospatial/CityGML-3.0Encodings).

Please note that CityGML 3.0 has not yet been published (but is quite stable now). Therefore, the XML schemata may be still subject to change.</br>


## Test data sets
The workspace was tested using the "FZK Haus" data set from: http://www.ifcwiki.org/index.php?title=KIT_IFC_Examples </br>
The data set is provided in the 'input' folder of this repository. </br>
The CityGML 3.0 data set created by the FME workspace is available in the 'output' folder.


## Mapping of IFC objects to CityGML 3.0 objects
The table below shows the mapping of IFC objects to the corresponding objects in CityGML 3.0. </br>
The mapping makes use of the class 'BuildingConstructiveElement' that was newly introduced to CityGML 3.0 to allow for representing constructive elements from BIM datasets given in the IFC standard (e.g. the IFC classes 'IfcWall', 'IfcRoof', 'IfcBeam', 'IfcSlab', etc.) in CityGML.

| IFC objects         | CityGML 3.0 objects           |
| ------------------- | --------------------------- |
| IfcProject          | CityModel                   |
| IfcSite             | LandUse                     |
| IfcBuilding         | Building                    |
| IfcBuildingStorey   | Storey                      |
| IfcSpace            | BuildingRoom                |
| IfcWallStandardCase | BuildingConstructiveElement |
| IfcBeam             | BuildingConstructiveElement |
| IfcSlab             | BuildingConstructiveElement |
| IfcMember           | BuildingConstructiveElement |
| IfcDoor             | Door                        |
| IfcWindow           | Window                      |
| IfcRailing          | BuildingInstallation        |
| IfcStair            | BuildingInstallation        |


## Results
Below are some screenshots of the transformed 'FZKHaus' data set visualised using the FME Data Inspector 2019.0.

FZKHaus represented in CityGML 3.0:
![FZKHaus represented in CityGML 3.0](images/FZKHaus.jpg "FZKHaus represented in CityGML 3.0")

FZKHaus - Rooms:
![FZKHaus - Rooms](images/FZKHaus_Rooms.jpg "FZKHaus - Rooms")

FZKHaus - BuildingInstallations, Doors, and Windows:
![FZKHaus - BuildingInstallations, Doors, and Windows](images/FZKHaus_BuildingInstallations_Doors_Windows.jpg "FZKHaus - BuildingInstallations, Doors, and Windows")

FZKHaus - BuildingConstructiveElements:
![FZKHaus - BuildingConstructiveElements](images/FZKHaus_BuildingConstructiveElements.jpg "FZKHaus - BuildingConstructiveElements")
