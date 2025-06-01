# HelmPack - Universal Helm Chart Bundler

🚀 **A powerful tool for packaging ANY Helm chart with all dependencies and images for air-gapped deployment.**

Unlike VMware's distribution-tooling-for-helm which only works with properly annotated OCI charts, HelmPack can analyze and bundle **any** Helm chart by intelligently discovering all container images and dependencies.

## ✨ Features

- **Universal Compatibility**: Works with any Helm chart, not just OCI or annotated ones
- **Intelligent Image Discovery**: Multiple methods to find container images:
  - Parse Chart.yaml annotations (when available)
  - Render templates and extract image references
  - Analyze values.yaml files
  - Manual template parsing for complex charts
- **Complete Dependency Resolution**: Recursively analyzes sub-charts
- **Air-Gap Ready**: Creates self-contained bundles for offline deployment
- **Harbor Integration**: Direct import to Harbor registries with automatic image relocation
- **Easy CLI**: Simple commands for bundle creation and import

## 🛠️ Installation

### Prerequisites

- Python 3.7+
- Docker (for image operations)
- Helm 3.x
- Access to source registries (for bundling)
- Harbor registry (for air-gapped import)

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Make Executable

```bash
chmod +x helmpack.py
```

## 🚀 Quick Start

### 1. Analyze a Chart

First, see what HelmPack discovers in your chart:

```bash
./helmpack.py analyze bitnami/wordpress
# or
./helmpack.py analyze ./my-local-chart
# or  
./helmpack.py analyze oci://registry.com/charts/app
```

### 2. Create a Bundle

Package everything for air-gapped deployment:

```bash
# Bundle from Helm repository
./helmpack.py bundle bitnami/wordpress --output ./bundles

# Bundle from OCI registry
./helmpack.py bundle oci://docker.io/bitnamicharts/mysql

# Bundle local chart
./helmpack.py bundle ./my-chart --output ./bundles

# Bundle without pulling images (chart only)
./helmpack.py bundle bitnami/nginx --no-images
```

### 3. Transfer to Air-Gapped Environment

Copy the generated `.helmpack.tgz` file to your air-gapped environment via approved methods (USB, secure transfer, etc.).

### 4. Import to Harbor Registry

In your air-gapped environment:

```bash
# Test Harbor connectivity first
./helmpack.py test-harbor \
  --harbor-url https://harbor.company.com \
  --harbor-user admin \
  --harbor-password mypassword

# Import the bundle
./helmpack.py import-bundle wordpress-15.2.5.helmpack.tgz \
  --harbor-url https://harbor.company.com \
  --harbor-user admin \
  --harbor-password mypassword \
  --project myproject
```

### 5. Deploy in Air-Gapped Environment

```bash
helm install my-wordpress oci://harbor.company.com/myproject/wordpress
```

## 📋 Commands Reference

### `analyze`
Analyze a chart and show discovered images and dependencies.

```bash
./helmpack.py analyze CHART
```

**Examples:**
```bash
./helmpack.py analyze bitnami/wordpress
./helmpack.py analyze ./local-chart
./helmpack.py analyze oci://registry.com/charts/app
```

### `bundle`
Create a complete bundle for air-gapped deployment.

```bash
./helmpack.py bundle CHART [OPTIONS]
```

**Options:**
- `--output, -o`: Output directory (default: current directory)
- `--no-images`: Skip pulling container images 
- `--include-signatures`: Include image signatures and metadata
- `--verbose, -v`: Enable verbose logging

**Examples:**
```bash
# Basic bundle
./helmpack.py bundle bitnami/mysql

# Bundle to specific directory
./helmpack.py bundle bitnami/postgresql --output /tmp/bundles

# Chart-only bundle (no images)
./helmpack.py bundle ./my-chart --no-images

# Include signatures and metadata
./helmpack.py bundle bitnami/redis --include-signatures
```

### `import-bundle`
Import a bundle into Harbor registry.

```bash
./helmpack.py import-bundle BUNDLE_PATH [OPTIONS]
```

**Options:**
- `--harbor-url`: Harbor registry URL (required)
- `--harbor-user`: Harbor username (required)
- `--harbor-password`: Harbor password (required)
- `--project`: Harbor project name (default: library)

**Example:**
```bash
./helmpack.py import-bundle wordpress-15.2.5.helmpack.tgz \
  --harbor-url https://harbor.internal.com \
  --harbor-user admin \
  --harbor-password secret123 \
  --project applications
```

### `info`
Show information about a bundle without importing it.

```bash
./helmpack.py info BUNDLE_PATH
```

**Example:**
```bash
./helmpack.py info wordpress-15.2.5.helmpack.tgz
```

### `test-harbor`
Test connectivity to Harbor registry.

```bash
./helmpack.py test-harbor [OPTIONS]
```

**Options:**
- `--harbor-url`: Harbor registry URL (required)
- `--harbor-user`: Harbor username (required)
- `--harbor-password`: Harbor password (required)

## 🏗️ How It Works

### Image Discovery Methods

HelmPack uses multiple sophisticated methods to discover container images:

1. **Annotation Parsing**: Reads existing `images` or `artifacthub.io/images` annotations
2. **Template Rendering**: Uses `helm template` to render charts and extract image references
3. **Values Analysis**: Recursively searches values.yaml files for image configurations
4. **Manual Parsing**: Regex-based parsing of template files for image references

### Bundle Structure

Each bundle contains:
```
chart-name-version.helmpack.tgz
├── bundle.yaml          # Metadata and image inventory
├── chart/               # Complete chart with dependencies
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── templates/
│   └── charts/          # Sub-chart dependencies
└── images/              # Container image archives (optional)
    ├── image1.tar
    ├── image2.tar
    └── ...
```

### Import Process

During import, HelmPack:
1. Extracts the bundle
2. Loads all container images into local Docker
3. Re-tags images for Harbor registry
4. Pushes images to Harbor
5. Relocates image references in chart files
6. Packages and pushes the updated chart to Harbor

## 🔧 Advanced Usage

### Working with Private Registries

For charts that reference private registries, ensure Docker is logged in before bundling:

```bash
docker login private-registry.com
./helmpack.py bundle oci://private-registry.com/charts/app
```

### Custom Values During Analysis

For charts with conditional image logic, you can render with custom values:

```bash
# Create custom values file
echo "feature.enabled: true" > custom-values.yaml

# Helm will use these during template rendering
./helmpack.py bundle ./chart --values custom-values.yaml
```

### Large Bundles

For charts with many large images, consider:

```bash
# Create chart-only bundle first
./helmpack.py bundle bitnami/wordpress --no-images

# Then manually pull specific images you need
docker pull wordpress:latest
docker save wordpress:latest > wordpress.tar
```

## 🐛 Troubleshooting

### Common Issues

**1. "Chart not found"**
- Ensure Helm can access the chart: `helm show chart CHART`
- For OCI charts, ensure proper authentication

**2. "Failed to pull image"**
- Check Docker daemon is running
- Verify registry authentication: `docker login`
- Some images may not be publicly accessible

**3. "Harbor import failed"**
- Test Harbor connectivity: `./helmpack.py test-harbor ...`
- Verify Harbor project exists and user has push permissions
- Check Harbor storage quota

**4. "Template rendering failed"**
- Chart may have missing required values
- Try analyzing the chart first: `./helmpack.py analyze CHART`

### Debug Mode

Enable verbose logging for detailed troubleshooting:

```bash
./helmpack.py --verbose bundle bitnami/mysql
```

## 🤝 Contributing

This is a powerful foundation that can be extended with:

- Additional image discovery methods
- Support for other registry types (AWS ECR, GCR, etc.)
- Helm chart signing and verification
- GUI interface
- Integration with CI/CD pipelines

## 📄 License

This tool is designed for enterprise air-gapped deployments and follows cloud-native best practices.

## 🆚 Comparison with VMware dt

| Feature | HelmPack | VMware dt |
|---------|----------|-----------|
| Chart Compatibility | **Any Helm chart** | OCI + annotated only |
| Image Discovery | **Multi-method intelligent** | Annotation-based |
| Dependencies | **Full recursive analysis** | Limited |
| Harbor Integration | **Built-in** | Manual |
| Learning Curve | **Python-friendly** | Go-based |
| Customization | **Highly extensible** | Limited |

HelmPack fills the gap for organizations that need to work with the vast ecosystem of existing Helm charts that aren't properly annotated for the VMware tool.

---

🎉 **Ready to simplify your air-gapped Helm deployments? Give HelmPack a try!**
