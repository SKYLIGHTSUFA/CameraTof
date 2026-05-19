# ==========================================================
# Runtime image with Daheng Galaxy SDK + Harvesters
#
# Requires the Galaxy Linux SDK directory to be present in the
# build context at:
#   Galaxy_Linux-x86_Gige-U3_32bits-64bits_2.4.2507.9231/Galaxy_camera/lib/x86_64/
#
# Download the SDK from Daheng Imaging and unpack it next to this Dockerfile.
# ==========================================================
FROM sazonovanton/ffmpeg-opencv-cuda:12.1.1-cudnn8-runtime-python3.11

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Galaxy SDK paths
ENV GALAXY_SDK=/opt/galaxy_sdk
ENV GENICAM_GENTL64_PATH=/opt/galaxy_sdk/lib/x86_64
ENV LD_LIBRARY_PATH=/opt/galaxy_sdk/lib/x86_64:/opt/python3/lib:/usr/local/lib:/usr/local/lib64:/usr/local/cuda/lib64:$LD_LIBRARY_PATH

# Runtime deps (libstdc++ / libgomp already in base; add net-tools for diagnostics)
RUN apt-get update && apt-get install -y --no-install-recommends \
    net-tools \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Copy Galaxy SDK shared libraries and CTI files into the image
COPY Galaxy_Linux-x86_Gige-U3_32bits-64bits_2.4.2507.9231/Galaxy_camera/lib/x86_64/ /opt/galaxy_sdk/lib/x86_64/

# Register Galaxy SDK libraries with the dynamic linker
RUN echo "/opt/galaxy_sdk/lib/x86_64" > /etc/ld.so.conf.d/galaxy.conf && ldconfig

# Create HLS output dir
RUN mkdir -p /app/hls_output && chmod 777 /app/hls_output

# Install Python dependencies (skip OpenCV — already in base image)
COPY requirements.txt /tmp/requirements.txt
RUN grep -iv "opencv" /tmp/requirements.txt > /tmp/req_no_cv.txt && \
    pip install --no-cache-dir -r /tmp/req_no_cv.txt

# Smoke test: harvesters can import and load the CTI without cameras present
RUN python3 -c "\
from harvesters.core import Harvester; \
h = Harvester(); \
h.add_file('/opt/galaxy_sdk/lib/x86_64/GxGVTL.cti'); \
h.update(); \
print('Harvesters + Galaxy CTI OK, devices visible without cameras:', len(h.device_info_list)); \
h.reset()"

WORKDIR /app

CMD ["python3", "-u", "app.py"]