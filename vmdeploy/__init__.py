"""vmdeploy — control-plane core for deploying VMs from vCenter templates.

Wraps govc (subprocess) and renders cloud-init. Shared by the FastAPI app and
any CLI. The template-BUILD path stays in the bash toolkit; this package only
lists templates and deploys VMs from them.
"""
