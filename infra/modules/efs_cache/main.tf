resource "aws_security_group" "efs" {
  name        = "${var.name}-efs-sg"
  description = "EFS access for Lambda repo cache"
  vpc_id      = var.vpc_id

  dynamic "ingress" {
    for_each = toset(var.allowed_sg_ids)
    content {
      description     = "NFS from allowed security group"
      from_port       = 2049
      to_port         = 2049
      protocol        = "tcp"
      security_groups = [ingress.value]
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name = "${var.name}-efs-sg"
  })
}

resource "aws_efs_file_system" "this" {
  creation_token = "${var.name}-efs-cache"
  encrypted      = true

  tags = merge(var.tags, {
    Name = "${var.name}-efs-cache"
  })
}

resource "aws_efs_mount_target" "this" {
  for_each = {
    for subnet_id in var.subnet_ids : subnet_id => subnet_id
  }

  file_system_id  = aws_efs_file_system.this.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "this" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = var.posix_uid
    gid = var.posix_gid
  }

  root_directory {
    path = var.access_point_path

    creation_info {
      owner_uid   = var.posix_uid
      owner_gid   = var.posix_gid
      permissions = var.root_permissions
    }
  }

  tags = merge(var.tags, {
    Name = "${var.name}-efs-access-point"
  })
}
