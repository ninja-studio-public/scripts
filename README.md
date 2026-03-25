# `scripts` repo

1. Contains publicly accessible scripts.
2. For this reason, do not store any sensitive information in this repo.
3. The plan is to only access these scripts via other repos in the `ninja-studio-public` organization.
4. This way, breaking changes are limited in scope and can be fixed entirely within the organizatiom.
5. For example, a restructuring of this repo will change the checkout paths when the scripts are used by, say, the `workflows` repo.
6. In this situation, only the `yaml` files in the `workflows` repo will need to be updated. Projects that call those workflows from external repos do not need to change anything.
