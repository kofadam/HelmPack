#!/usr/bin/env python3
"""
Universal Helm Chart Bundler for Air-Gapped Environments
A tool to package any Helm chart with all dependencies and images for offline deployment.
"""

import os
import sys
import json
import yaml
import tarfile
import tempfile
import subprocess
import shutil
import re
import logging
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
from dataclasses import dataclass, asdict
from urllib.parse import urlparse
import click
import requests
from docker import from_env as docker_from_env
from docker.errors import ImageNotFound, APIError

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class ImageInfo:
    """Information about a discovered container image."""
    name: str
    tag: str
    registry: str
    repository: str
    full_reference: str
    chart_source: str
    digest: Optional[str] = None
    size: Optional[int] = None

@dataclass
class ChartInfo:
    """Information about a Helm chart."""
    name: str
    version: str
    path: str
    dependencies: List['ChartInfo']
    images: List[ImageInfo]

class HelmChartAnalyzer:
    """Analyzes Helm charts to discover all images and dependencies."""
    
    def __init__(self):
        self.docker_client = None
        self.discovered_images: Set[str] = set()
        self.temp_dirs: List[str] = []
        
    def __enter__(self):
        try:
            self.docker_client = docker_from_env()
        except Exception as e:
            logger.warning(f"Docker client not available: {e}")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Cleanup temporary directories
        for temp_dir in self.temp_dirs:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
    
    def analyze_chart(self, chart_path_or_url: str) -> ChartInfo:
        """Analyze a Helm chart and return comprehensive information."""
        logger.info(f"🔍 Analyzing chart: {chart_path_or_url}")
        
        # Download or copy chart to temporary location
        chart_path = self._prepare_chart(chart_path_or_url)
        
        # Parse Chart.yaml
        chart_yaml_path = os.path.join(chart_path, "Chart.yaml")
        if not os.path.exists(chart_yaml_path):
            raise ValueError(f"Chart.yaml not found in {chart_path}")
            
        with open(chart_yaml_path, 'r') as f:
            chart_yaml = yaml.safe_load(f)
        
        chart_name = chart_yaml.get('name', 'unknown')
        chart_version = chart_yaml.get('version', '0.0.0')
        
        logger.info(f"📊 Chart: {chart_name} v{chart_version}")
        
        # Discover dependencies
        dependencies = self._discover_dependencies(chart_path, chart_yaml)
        
        # Discover images from this chart
        images = self._discover_images(chart_path, chart_name)
        
        # Combine images from dependencies
        all_images = images.copy()
        for dep in dependencies:
            all_images.extend(dep.images)
        
        return ChartInfo(
            name=chart_name,
            version=chart_version,
            path=chart_path,
            dependencies=dependencies,
            images=all_images
        )
    
    def _prepare_chart(self, chart_path_or_url: str) -> str:
        """Prepare chart for analysis (download if URL, copy if local)."""
        if chart_path_or_url.startswith(('http://', 'https://', 'oci://')):
            return self._download_chart(chart_path_or_url)
        elif os.path.isdir(chart_path_or_url):
            return chart_path_or_url
        elif chart_path_or_url.endswith('.tgz'):
            return self._extract_chart_archive(chart_path_or_url)
        else:
            raise ValueError(f"Unsupported chart source: {chart_path_or_url}")
    
    def _download_chart(self, chart_url: str) -> str:
        """Download a chart from a repository or OCI registry."""
        temp_dir = tempfile.mkdtemp(prefix="helmpack_")
        self.temp_dirs.append(temp_dir)
        
        try:
            if chart_url.startswith('oci://'):
                # Use helm pull for OCI charts
                cmd = ['helm', 'pull', chart_url, '--untar', '--destination', temp_dir]
            else:
                # For HTTP URLs, try helm pull as well
                cmd = ['helm', 'pull', chart_url, '--untar', '--destination', temp_dir]
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            logger.info(f"✅ Chart downloaded to {temp_dir}")
            
            # Find the extracted chart directory
            chart_dirs = [d for d in os.listdir(temp_dir) if os.path.isdir(os.path.join(temp_dir, d))]
            if not chart_dirs:
                raise ValueError("No chart directory found after extraction")
            
            return os.path.join(temp_dir, chart_dirs[0])
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to download chart: {e.stderr}")
            raise
    
    def _extract_chart_archive(self, archive_path: str) -> str:
        """Extract a chart archive (.tgz) to temporary directory."""
        temp_dir = tempfile.mkdtemp(prefix="helmpack_")
        self.temp_dirs.append(temp_dir)
        
        with tarfile.open(archive_path, 'r:gz') as tar:
            tar.extractall(temp_dir)
        
        # Find the extracted chart directory
        chart_dirs = [d for d in os.listdir(temp_dir) if os.path.isdir(os.path.join(temp_dir, d))]
        if not chart_dirs:
            raise ValueError("No chart directory found in archive")
        
        return os.path.join(temp_dir, chart_dirs[0])
    
    def _discover_dependencies(self, chart_path: str, chart_yaml: dict) -> List[ChartInfo]:
        """Discover and analyze chart dependencies."""
        dependencies = []
        deps = chart_yaml.get('dependencies', [])
        
        if not deps:
            return dependencies
        
        logger.info(f"🔗 Found {len(deps)} dependencies")
        
        # Update dependencies
        try:
            subprocess.run(['helm', 'dependency', 'update', chart_path], 
                         capture_output=True, check=True, cwd=chart_path)
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to update dependencies: {e}")
        
        # Analyze each dependency
        charts_dir = os.path.join(chart_path, 'charts')
        if os.path.exists(charts_dir):
            for dep_file in os.listdir(charts_dir):
                if dep_file.endswith('.tgz'):
                    dep_path = os.path.join(charts_dir, dep_file)
                    try:
                        dep_chart = self.analyze_chart(dep_path)
                        dependencies.append(dep_chart)
                        logger.info(f"  📦 Dependency: {dep_chart.name} v{dep_chart.version}")
                    except Exception as e:
                        logger.warning(f"Failed to analyze dependency {dep_file}: {e}")
        
        return dependencies
    
    def _discover_images(self, chart_path: str, chart_name: str) -> List[ImageInfo]:
        """Discover all container images referenced in the chart."""
        logger.info(f"🔍 Discovering images in chart: {chart_name}")
        
        images = []
        
        # Method 1: Parse existing annotations (if any)
        images.extend(self._parse_chart_annotations(chart_path, chart_name))
        
        # Method 2: Render templates and extract images
        images.extend(self._extract_images_from_templates(chart_path, chart_name))
        
        # Method 3: Parse values.yaml for image references
        images.extend(self._parse_values_for_images(chart_path, chart_name))
        
        # Remove duplicates
        unique_images = {}
        for img in images:
            unique_images[img.full_reference] = img
        
        result = list(unique_images.values())
        logger.info(f"✅ Found {len(result)} unique images in {chart_name}")
        
        return result
    
    def _parse_chart_annotations(self, chart_path: str, chart_name: str) -> List[ImageInfo]:
        """Parse images from Chart.yaml annotations."""
        chart_yaml_path = os.path.join(chart_path, "Chart.yaml")
        with open(chart_yaml_path, 'r') as f:
            chart_yaml = yaml.safe_load(f)
        
        images = []
        annotations = chart_yaml.get('annotations', {})
        
        # Check for images annotation
        images_annotation = annotations.get('images')
        if images_annotation:
            try:
                if isinstance(images_annotation, str):
                    # YAML string format
                    image_list = yaml.safe_load(images_annotation)
                else:
                    image_list = images_annotation
                
                for item in image_list:
                    if isinstance(item, dict) and 'image' in item:
                        img_info = self._parse_image_reference(item['image'], chart_name)
                        if img_info:
                            images.append(img_info)
            except Exception as e:
                logger.warning(f"Failed to parse images annotation: {e}")
        
        # Check for artifacthub.io/images annotation
        artifacthub_images = annotations.get('artifacthub.io/images')
        if artifacthub_images:
            try:
                image_list = yaml.safe_load(artifacthub_images)
                for item in image_list:
                    if isinstance(item, dict) and 'image' in item:
                        img_info = self._parse_image_reference(item['image'], chart_name)
                        if img_info:
                            images.append(img_info)
            except Exception as e:
                logger.warning(f"Failed to parse artifacthub.io/images annotation: {e}")
        
        return images
    
    def _extract_images_from_templates(self, chart_path: str, chart_name: str) -> List[ImageInfo]:
        """Extract images by rendering Helm templates."""
        images = []
        
        try:
            # Render templates with default values
            cmd = ['helm', 'template', 'test-release', chart_path]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            # Parse rendered YAML to find image references
            documents = result.stdout.split('---')
            
            for doc in documents:
                if not doc.strip():
                    continue
                
                try:
                    yaml_doc = yaml.safe_load(doc)
                    if yaml_doc:
                        found_images = self._extract_images_from_yaml(yaml_doc, chart_name)
                        images.extend(found_images)
                except yaml.YAMLError:
                    continue
        
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to render templates for {chart_name}: {e.stderr}")
            # Fallback to manual template parsing
            images.extend(self._parse_templates_manually(chart_path, chart_name))
        
        return images
    
    def _parse_values_for_images(self, chart_path: str, chart_name: str) -> List[ImageInfo]:
        """Parse values.yaml files for image references."""
        images = []
        
        values_files = ['values.yaml', 'values.yml']
        for values_file in values_files:
            values_path = os.path.join(chart_path, values_file)
            if os.path.exists(values_path):
                with open(values_path, 'r') as f:
                    try:
                        values = yaml.safe_load(f)
                        found_images = self._extract_images_from_yaml(values, chart_name)
                        images.extend(found_images)
                    except yaml.YAMLError as e:
                        logger.warning(f"Failed to parse {values_file}: {e}")
        
        return images
    
    def _parse_templates_manually(self, chart_path: str, chart_name: str) -> List[ImageInfo]:
        """Manually parse template files for image references."""
        images = []
        templates_dir = os.path.join(chart_path, 'templates')
        
        if not os.path.exists(templates_dir):
            return images
        
        # Common image reference patterns
        image_patterns = [
            r'image:\s*["\']?([^"\s]+)["\']?',
            r'Image:\s*["\']?([^"\s]+)["\']?',
            r'\.image\s*["\']?([^"\s]+)["\']?',
            r'\.Image\s*["\']?([^"\s]+)["\']?',
        ]
        
        for root, dirs, files in os.walk(templates_dir):
            for file in files:
                if file.endswith(('.yaml', '.yml')):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r') as f:
                            content = f.read()
                            
                            for pattern in image_patterns:
                                matches = re.finditer(pattern, content, re.IGNORECASE)
                                for match in matches:
                                    image_ref = match.group(1)
                                    # Skip template variables
                                    if not ('{{' in image_ref or '}}' in image_ref):
                                        img_info = self._parse_image_reference(image_ref, chart_name)
                                        if img_info:
                                            images.append(img_info)
                    except Exception as e:
                        logger.warning(f"Failed to parse template {file_path}: {e}")
        
        return images
    
    def _extract_images_from_yaml(self, yaml_obj, chart_name: str) -> List[ImageInfo]:
        """Recursively extract image references from YAML object."""
        images = []
        
        if isinstance(yaml_obj, dict):
            for key, value in yaml_obj.items():
                if key.lower() == 'image' and isinstance(value, str):
                    img_info = self._parse_image_reference(value, chart_name)
                    if img_info:
                        images.append(img_info)
                elif isinstance(value, (dict, list)):
                    images.extend(self._extract_images_from_yaml(value, chart_name))
        elif isinstance(yaml_obj, list):
            for item in yaml_obj:
                if isinstance(item, (dict, list)):
                    images.extend(self._extract_images_from_yaml(item, chart_name))
        
        return images
    
    def _parse_image_reference(self, image_ref: str, chart_source: str) -> Optional[ImageInfo]:
        """Parse a container image reference into components."""
        if not image_ref or image_ref.startswith('{{'):
            return None
        
        # Clean up the reference
        image_ref = image_ref.strip().strip('"\'')
        
        # Skip obviously invalid references
        if not image_ref or '{{' in image_ref or '}}' in image_ref:
            return None
        
        # Parse registry, repository, and tag
        parts = image_ref.split('/')
        
        if '.' in parts[0] or ':' in parts[0]:
            # Has registry
            registry = parts[0]
            repo_parts = parts[1:]
        else:
            # No registry, use Docker Hub
            registry = 'docker.io'
            repo_parts = parts
        
        if not repo_parts:
            return None
        
        # Handle tag
        last_part = repo_parts[-1]
        if ':' in last_part:
            repo_parts[-1], tag = last_part.rsplit(':', 1)
        else:
            tag = 'latest'
        
        repository = '/'.join(repo_parts)
        name = repo_parts[-1]
        
        return ImageInfo(
            name=name,
            tag=tag,
            registry=registry,
            repository=repository,
            full_reference=image_ref,
            chart_source=chart_source
        )

class HelmPackBundler:
    """Creates bundles for air-gapped deployment."""
    
    def __init__(self):
        self.docker_client = None
        
    def __enter__(self):
        try:
            self.docker_client = docker_from_env()
        except Exception as e:
            logger.warning(f"Docker client not available: {e}")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
    
    def create_bundle(self, chart_info: ChartInfo, output_path: str, 
                     pull_images: bool = True, include_signatures: bool = False) -> str:
        """Create a complete bundle for air-gapped deployment."""
        logger.info(f"📦 Creating bundle for {chart_info.name} v{chart_info.version}")
        
        bundle_name = f"{chart_info.name}-{chart_info.version}.helmpack.tgz"
        if output_path:
            # Create output directory if it doesn't exist
            os.makedirs(output_path, exist_ok=True)
            bundle_path = os.path.join(output_path, bundle_name)
        else:
            bundle_path = bundle_name
        
        with tempfile.TemporaryDirectory(prefix="helmpack_bundle_") as temp_dir:
            bundle_dir = os.path.join(temp_dir, f"{chart_info.name}-{chart_info.version}")
            os.makedirs(bundle_dir, exist_ok=True)
            
            # Create bundle metadata
            metadata = {
                "apiVersion": "v1",
                "kind": "HelmPackBundle",
                "metadata": {
                    "name": chart_info.name,
                    "version": chart_info.version,
                    "generatedAt": subprocess.run(['date', '-Iseconds'], 
                                                capture_output=True, text=True).stdout.strip(),
                    "generatedBy": "HelmPack Universal Bundler",
                    "bundlePath": bundle_path,
                    "totalImages": len(chart_info.images),
                    "totalDependencies": len(chart_info.dependencies)
                },
                "chart": asdict(chart_info),
                "images": [asdict(img) for img in chart_info.images]
            }
            
            # Ensure bundle directory exists
            os.makedirs(bundle_dir, exist_ok=True)
            
            with open(os.path.join(bundle_dir, "bundle.yaml"), 'w') as f:
                yaml.safe_dump(metadata, f, default_flow_style=False)
            
            # Copy chart files
            chart_dir = os.path.join(bundle_dir, "chart")
            shutil.copytree(chart_info.path, chart_dir)
            
            # Pull and save images if requested
            if pull_images and chart_info.images:
                self._pull_and_save_images(chart_info.images, bundle_dir)
            
            # Create tarball
            with tarfile.open(bundle_path, 'w:gz') as tar:
                tar.add(bundle_dir, arcname=os.path.basename(bundle_dir))
        
        logger.info(f"🎉 Bundle created: {bundle_path}")
        return bundle_path
    
    def _pull_and_save_images(self, images: List[ImageInfo], bundle_dir: str):
        """Pull and save container images."""
        if not self.docker_client:
            logger.warning("⚠️  Docker not available, skipping image pull")
            return
        
        images_dir = os.path.join(bundle_dir, "images")
        os.makedirs(images_dir, exist_ok=True)
        
        logger.info(f"🐳 Pulling {len(images)} images...")
        
        for i, image in enumerate(images, 1):
            try:
                logger.info(f"  [{i}/{len(images)}] Pulling {image.full_reference}")
                
                # Pull image
                pulled_image = self.docker_client.images.pull(image.full_reference)
                
                # Save image to tar file
                image_filename = f"{image.full_reference.replace('/', '_').replace(':', '_')}.tar"
                image_path = os.path.join(images_dir, image_filename)
                
                with open(image_path, 'wb') as f:
                    for chunk in pulled_image.save():
                        f.write(chunk)
                
                # Update image info with digest
                image.digest = pulled_image.id
                
            except (ImageNotFound, APIError) as e:
                logger.error(f"❌ Failed to pull {image.full_reference}: {e}")
            except Exception as e:
                logger.error(f"❌ Unexpected error pulling {image.full_reference}: {e}")

class HelmPackImporter:
    """Imports bundles into air-gapped environments."""
    
    def __init__(self, harbor_url: str, harbor_username: str, harbor_password: str, insecure: bool = False):
        self.harbor_url = harbor_url.rstrip('/')
        self.harbor_username = harbor_username
        self.harbor_password = harbor_password
        self.insecure = insecure
        self.docker_client = None
        
        if insecure:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
    def __enter__(self):
        try:
            self.docker_client = docker_from_env()
        except Exception as e:
            logger.warning(f"Docker client not available: {e}")
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass
    
    def import_bundle(self, bundle_path: str, target_project: str = "library"):
        """Import a bundle into Harbor registry."""
        logger.info(f"📥 Importing bundle: {bundle_path}")
        
        with tempfile.TemporaryDirectory(prefix="helmpack_import_") as temp_dir:
            # Extract bundle
            with tarfile.open(bundle_path, 'r:gz') as tar:
                tar.extractall(temp_dir)
            
            # Find bundle directory
            bundle_dirs = [d for d in os.listdir(temp_dir) 
                          if os.path.isdir(os.path.join(temp_dir, d))]
            if not bundle_dirs:
                raise ValueError("No bundle directory found in archive")
            
            bundle_dir = os.path.join(temp_dir, bundle_dirs[0])
            
            # Load metadata
            metadata_path = os.path.join(bundle_dir, "bundle.yaml")
            with open(metadata_path, 'r') as f:
                metadata = yaml.safe_load(f)
            
            chart_info = metadata['chart']
            images_info = metadata['images']
            
            # Import images
            self._import_images(bundle_dir, images_info, target_project)
            
            # Import chart
            self._import_chart(bundle_dir, chart_info, target_project, images_info)
        
        logger.info("🎉 Bundle imported successfully!")
    
    def _import_images(self, bundle_dir: str, images_info: List[dict], target_project: str):
        """Import container images into Harbor."""
        if not self.docker_client:
            logger.warning("⚠️  Docker not available, skipping image import")
            return
        
        images_dir = os.path.join(bundle_dir, "images")
        if not os.path.exists(images_dir):
            logger.info("📦 No images directory found, skipping image import")
            return
        
        logger.info(f"🐳 Importing {len(images_info)} images to Harbor...")
        
        # Login to Harbor
        try:
            self.docker_client.login(
                username=self.harbor_username,
                password=self.harbor_password,
                registry=self.harbor_url.split('://')[-1]
            )
        except Exception as e:
            logger.error(f"❌ Failed to login to Harbor: {e}")
            return
        
        for image_info in images_info:
            try:
                original_ref = image_info['full_reference']
                
                # Generate Harbor reference
                harbor_ref = self._generate_harbor_reference(original_ref, target_project)
                
                # Load image from tar
                image_filename = f"{original_ref.replace('/', '_').replace(':', '_')}.tar"
                image_path = os.path.join(images_dir, image_filename)
                
                if os.path.exists(image_path):
                    with open(image_path, 'rb') as f:
                        images = self.docker_client.images.load(f.read())
                    
                    if images:
                        loaded_image = images[0]
                        
                        # Tag for Harbor
                        loaded_image.tag(harbor_ref)
                        
                        # Push to Harbor
                        logger.info(f"  📤 Pushing {harbor_ref}")
                        self.docker_client.images.push(harbor_ref)
                        
                        # Clean up local image
                        self.docker_client.images.remove(loaded_image.id, force=True)
                
            except Exception as e:
                logger.error(f"❌ Failed to import image {original_ref}: {e}")
    
    def _import_chart(self, bundle_dir: str, chart_info: dict, target_project: str, images_info: List[dict]):
        """Import Helm chart with relocated image references."""
        chart_dir = os.path.join(bundle_dir, "chart")
        
        if not os.path.exists(chart_dir):
            logger.error("❌ No chart directory found in bundle")
            return
        
        logger.info(f"📊 Importing chart {chart_info['name']} v{chart_info['version']}")
        
        # Create image mapping for relocation
        image_mapping = {}
        for image_info in images_info:
            original_ref = image_info['full_reference']
            harbor_ref = self._generate_harbor_reference(original_ref, target_project)
            image_mapping[original_ref] = harbor_ref
        
        # Relocate image references in chart
        self._relocate_chart_images(chart_dir, image_mapping)
        
        # Package and push chart to Harbor
        self._push_chart_to_harbor(chart_dir, target_project)
    
    def _generate_harbor_reference(self, original_ref: str, target_project: str) -> str:
        """Generate Harbor registry reference for an image."""
        harbor_host = self.harbor_url.split('://')[-1]
        
        # Parse original reference
        parts = original_ref.split('/')
        if ':' in parts[-1]:
            repo_tag = parts[-1].rsplit(':', 1)
            repo = repo_tag[0]
            tag = repo_tag[1]
        else:
            repo = parts[-1]
            tag = 'latest'
        
        return f"{harbor_host}/{target_project}/{repo}:{tag}"
    
    def _relocate_chart_images(self, chart_dir: str, image_mapping: dict):
        """Relocate image references in chart files."""
        logger.info("🔄 Relocating image references...")
        
        # Update values.yaml files
        for values_file in ['values.yaml', 'values.yml']:
            values_path = os.path.join(chart_dir, values_file)
            if os.path.exists(values_path):
                self._relocate_images_in_file(values_path, image_mapping)
        
        # Update template files
        templates_dir = os.path.join(chart_dir, 'templates')
        if os.path.exists(templates_dir):
            for root, dirs, files in os.walk(templates_dir):
                for file in files:
                    if file.endswith(('.yaml', '.yml')):
                        file_path = os.path.join(root, file)
                        self._relocate_images_in_file(file_path, image_mapping)
    
    def _relocate_images_in_file(self, file_path: str, image_mapping: dict):
        """Relocate image references in a single file."""
        try:
            with open(file_path, 'r') as f:
                content = f.read()
            
            modified = False
            for original_ref, harbor_ref in image_mapping.items():
                if original_ref in content:
                    content = content.replace(original_ref, harbor_ref)
                    modified = True
            
            if modified:
                with open(file_path, 'w') as f:
                    f.write(content)
                logger.info(f"  ✅ Updated {file_path}")
        
        except Exception as e:
            logger.warning(f"Failed to relocate images in {file_path}: {e}")
    
    def _push_chart_to_harbor(self, chart_dir: str, target_project: str):
        """Package and push chart to Harbor registry."""
        try:
            harbor_host = self.harbor_url.split('://')[-1]
            
            # Package chart
            package_result = subprocess.run(
                ['helm', 'package', chart_dir],
                capture_output=True, text=True, check=True
            )
            
            # Find the packaged chart
            chart_files = [f for f in os.listdir('.') if f.endswith('.tgz')]
            if not chart_files:
                raise ValueError("No packaged chart found")
            
            chart_package = chart_files[-1]  # Get the latest
            
            # Push to Harbor using helm
            harbor_chart_url = f"oci://{harbor_host}/{target_project}"
            push_result = subprocess.run(
                ['helm', 'push', chart_package, harbor_chart_url],
                capture_output=True, text=True, check=True
            )
            
            logger.info(f"📊 Chart pushed to {harbor_chart_url}")
            
            # Clean up
            os.remove(chart_package)
            
        except subprocess.CalledProcessError as e:
            logger.error(f"❌ Failed to push chart to Harbor: {e.stderr}")
        except Exception as e:
            logger.error(f"❌ Unexpected error pushing chart: {e}")

# CLI Interface
@click.group()
@click.option('--verbose', '-v', is_flag=True, help='Enable verbose logging')
def cli(verbose):
    """Universal Helm Chart Bundler for Air-Gapped Environments"""
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

@cli.command()
@click.argument('chart')
@click.option('--output', '-o', default='.', help='Output directory for bundle')
@click.option('--no-images', is_flag=True, help='Skip pulling container images')
@click.option('--include-signatures', is_flag=True, help='Include image signatures and metadata')
def bundle(chart, output, no_images, include_signatures):
    """Create a bundle from a Helm chart for air-gapped deployment.
    
    CHART can be:
    - Local directory containing a chart
    - URL to a chart repository
    - OCI registry reference (oci://...)
    - Path to a .tgz chart archive
    """
    try:
        with HelmChartAnalyzer() as analyzer:
            chart_info = analyzer.analyze_chart(chart)
            
            with HelmPackBundler() as bundler:
                bundle_path = bundler.create_bundle(
                    chart_info=chart_info,
                    output_path=output,
                    pull_images=not no_images,
                    include_signatures=include_signatures
                )
                
                # Display summary
                click.echo("\n" + "="*60)
                click.echo(f"📦 Bundle Summary")
                click.echo("="*60)
                click.echo(f"Chart: {chart_info.name} v{chart_info.version}")
                click.echo(f"Dependencies: {len(chart_info.dependencies)}")
                click.echo(f"Total Images: {len(chart_info.images)}")
                click.echo(f"Bundle: {bundle_path}")
                
                if chart_info.images:
                    click.echo(f"\n🐳 Images included:")
                    for img in chart_info.images[:10]:  # Show first 10
                        click.echo(f"  • {img.full_reference} (from {img.chart_source})")
                    if len(chart_info.images) > 10:
                        click.echo(f"  ... and {len(chart_info.images) - 10} more")
                
                click.echo("\n✅ Bundle created successfully!")
                
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)

@cli.command()
@click.argument('bundle_path')
@click.option('--harbor-url', required=True, help='Harbor registry URL')
@click.option('--harbor-user', required=True, help='Harbor username')
@click.option('--harbor-password', required=True, help='Harbor password')
@click.option('--project', default='library', help='Harbor project name')
@click.option('--insecure', is_flag=True, help='Skip SSL certificate verification')
def import_bundle(bundle_path, harbor_url, harbor_user, harbor_password, project, insecure):
    """Import a bundle into Harbor registry for air-gapped deployment."""
    try:
        with HelmPackImporter(harbor_url, harbor_user, harbor_password, insecure) as importer:
            importer.import_bundle(bundle_path, project)
            
            click.echo("\n" + "="*60)
            click.echo("🎉 Import Complete!")
            click.echo("="*60)
            click.echo(f"Harbor Registry: {harbor_url}")
            click.echo(f"Project: {project}")
            click.echo("\nYou can now deploy charts using:")
            click.echo(f"helm install <release-name> oci://{harbor_url.split('://')[-1]}/{project}/<chart-name>")
            
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)

@cli.command()
@click.argument('chart')
def analyze(chart):
    """Analyze a Helm chart and show discovered images and dependencies."""
    try:
        with HelmChartAnalyzer() as analyzer:
            chart_info = analyzer.analyze_chart(chart)
            
            click.echo("\n" + "="*60)
            click.echo(f"📊 Chart Analysis: {chart_info.name} v{chart_info.version}")
            click.echo("="*60)
            
            click.echo(f"\n📦 Dependencies ({len(chart_info.dependencies)}):")
            if chart_info.dependencies:
                for dep in chart_info.dependencies:
                    click.echo(f"  • {dep.name} v{dep.version}")
            else:
                click.echo("  None")
            
            click.echo(f"\n🐳 Container Images ({len(chart_info.images)}):")
            if chart_info.images:
                # Group by chart source
                by_chart = {}
                for img in chart_info.images:
                    if img.chart_source not in by_chart:
                        by_chart[img.chart_source] = []
                    by_chart[img.chart_source].append(img)
                
                for chart_name, images in by_chart.items():
                    click.echo(f"\n  From {chart_name}:")
                    for img in images:
                        click.echo(f"    • {img.full_reference}")
            else:
                click.echo("  None found")
            
            click.echo(f"\n📍 Chart Location: {chart_info.path}")
            
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)

@cli.command()
@click.argument('bundle_path')
def info(bundle_path):
    """Show information about a bundle without importing it."""
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Extract bundle
            with tarfile.open(bundle_path, 'r:gz') as tar:
                tar.extractall(temp_dir)
            
            # Find bundle directory
            bundle_dirs = [d for d in os.listdir(temp_dir) 
                          if os.path.isdir(os.path.join(temp_dir, d))]
            if not bundle_dirs:
                raise ValueError("Invalid bundle: no bundle directory found")
            
            bundle_dir = os.path.join(temp_dir, bundle_dirs[0])
            
            # Load metadata
            metadata_path = os.path.join(bundle_dir, "bundle.yaml")
            with open(metadata_path, 'r') as f:
                metadata = yaml.safe_load(f)
            
            chart_info = metadata['chart']
            images_info = metadata['images']
            bundle_metadata = metadata['metadata']
            
            click.echo("\n" + "="*60)
            click.echo(f"📦 Bundle Information")
            click.echo("="*60)
            click.echo(f"Chart: {chart_info['name']} v{chart_info['version']}")
            click.echo(f"Generated: {bundle_metadata.get('generatedAt', 'Unknown')}")
            click.echo(f"Generated By: {bundle_metadata.get('generatedBy', 'Unknown')}")
            click.echo(f"Dependencies: {len(chart_info.get('dependencies', []))}")
            click.echo(f"Images: {len(images_info)}")
            
            # Check if images are included
            images_dir = os.path.join(bundle_dir, "images")
            if os.path.exists(images_dir):
                image_files = [f for f in os.listdir(images_dir) if f.endswith('.tar')]
                click.echo(f"Image Archives: {len(image_files)} files")
                
                # Calculate total size
                total_size = 0
                for img_file in image_files:
                    img_path = os.path.join(images_dir, img_file)
                    total_size += os.path.getsize(img_path)
                
                size_mb = total_size / (1024 * 1024)
                if size_mb > 1024:
                    click.echo(f"Images Size: {size_mb/1024:.1f} GB")
                else:
                    click.echo(f"Images Size: {size_mb:.1f} MB")
            else:
                click.echo("Image Archives: Not included")
            
            if images_info:
                click.echo(f"\n🐳 Container Images:")
                for img in images_info[:15]:  # Show first 15
                    click.echo(f"  • {img['full_reference']}")
                if len(images_info) > 15:
                    click.echo(f"  ... and {len(images_info) - 15} more")
            
    except Exception as e:
        click.echo(f"❌ Error: {e}", err=True)
        sys.exit(1)

@cli.command()
@click.option('--harbor-url', required=True, help='Harbor registry URL')
@click.option('--harbor-user', required=True, help='Harbor username')
@click.option('--harbor-password', required=True, help='Harbor password')
@click.option('--insecure', is_flag=True, help='Skip SSL certificate verification')
def test_harbor(harbor_url, harbor_user, harbor_password, insecure):
    """Test connectivity to Harbor registry."""
    try:
        # Test Harbor API
        harbor_host = harbor_url.rstrip('/')
        api_url = f"{harbor_host}/api/v2.0/systeminfo"
        
        # Configure SSL verification
        verify_ssl = not insecure
        if insecure:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            click.echo("⚠️  SSL certificate verification disabled")
        
        response = requests.get(
            api_url,
            auth=(harbor_user, harbor_password),
            timeout=10,
            verify=verify_ssl
        )
        
        if response.status_code == 200:
            click.echo(f"✅ Harbor API connection successful")
            system_info = response.json()
            click.echo(f"Harbor Version: {system_info.get('harbor_version', 'Unknown')}")
            click.echo(f"Registry URL: {system_info.get('registry_url', 'Unknown')}")
        else:
            click.echo(f"❌ Harbor API connection failed: {response.status_code}")
            if response.text:
                click.echo(f"Response: {response.text}")
            return
        
        # Test Docker registry login
        try:
            docker_client = docker_from_env()
            
            # For self-signed certs, we might need to configure Docker daemon
            registry_host = harbor_host.split('://')[-1]
            
            docker_client.login(
                username=harbor_user,
                password=harbor_password,
                registry=registry_host
            )
            click.echo(f"✅ Docker registry login successful")
            
            if insecure:
                click.echo("\n💡 Note: For Docker to work with self-signed certificates:")
                click.echo(f"   Add '{registry_host}' to Docker daemon's insecure-registries")
                click.echo("   or install the certificate in Docker's trust store")
                
        except Exception as e:
            click.echo(f"❌ Docker registry login failed: {e}")
            if "certificate" in str(e).lower() or "ssl" in str(e).lower():
                registry_host = harbor_host.split('://')[-1]
                click.echo(f"\n💡 For self-signed certificates, configure Docker:")
                click.echo(f"   1. Add to /etc/docker/daemon.json:")
                click.echo(f'      {{"insecure-registries": ["{registry_host}"]}}')
                click.echo(f"   2. Restart Docker: sudo systemctl restart docker")
                click.echo(f"   3. Or copy certificate to: /etc/docker/certs.d/{registry_host}/ca.crt")
        
        click.echo(f"\n🎉 Harbor connectivity test completed!")
        
    except Exception as e:
        if "certificate verify failed" in str(e) or "SSL" in str(e):
            click.echo(f"❌ SSL Certificate Error: {e}")
            click.echo(f"\n💡 Try again with --insecure flag to skip SSL verification:")
            click.echo(f"   ./helmpack.py test-harbor --insecure --harbor-url {harbor_url} --harbor-user {harbor_user} --harbor-password ***")
        else:
            click.echo(f"❌ Error testing Harbor: {e}", err=True)
        sys.exit(1)

if __name__ == '__main__':
    cli()
