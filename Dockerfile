FROM continuumio/miniconda3

LABEL maintainer=""

WORKDIR /app

# Copy project files
COPY . /app

# Update conda and install runtime dependencies from conda-forge
# ifcopenshell is available on conda-forge and provides prebuilt binaries
RUN conda update -n base -c defaults conda -y && \
    conda install -y -c conda-forge python=3.11 ifcopenshell lxml lark numpy && \
    conda clean -afy

# Install any additional pip requirements if provided
RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt || true; fi

# Default entry: run the converter. Pass IFC path and flags as arguments.
ENTRYPOINT ["python", "ifc2citygml.py"]
CMD ["--help"]
