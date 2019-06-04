# citygml2-to-citygml3
An FME workspace for converting IFC data sets to CityGML 3.0 data sets.

The FME workspace was created using FME 2019.0 (Build 19238). </br>
Opening the workspaces with other FME versions might lead to errors. 

The workspace was tested using the "FZK Haus" data set from: http://www.ifcwiki.org/index.php?title=KIT_IFC_Examples </br>
The data set is provided in the input folder of this repository. </br>
The CityGML 3.0 data set created by the FME workspace is available in the output folder.


### Mapping of IFC objects to CityGML 3.0 objects

| IFC objects         | CityGML 3 objects           |
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
| IfcDoor             | BuildingConstructiveElement |
| IfcWindow           | BuildingConstructiveElement |
| IfcRailing          | BuildingInstallation        |
| IfcStair            | BuildingInstallation        |
