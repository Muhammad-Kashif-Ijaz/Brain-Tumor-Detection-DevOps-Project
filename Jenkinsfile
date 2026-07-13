pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
  }

  environment {
    PROJECT_NAME = 'neuroscope'
    ENVIRONMENT = 'prod'
    AZURE_LOCATION = 'eastus'
    IMAGE_NAME = 'neuroscope-mri'
    IMAGE_TAG = "${env.BUILD_NUMBER}-${env.GIT_COMMIT ? env.GIT_COMMIT.take(7) : 'local'}"
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('Validate') {
      steps {
        sh 'bash scripts/ci_check.sh'
      }
    }

    stage('Provision and Deploy') {
      steps {
        withCredentials([string(credentialsId: 'azure-credentials-json', variable: 'AZURE_CREDENTIALS')]) {
          sh 'bash scripts/azure_deploy.sh'
        }
      }
    }
  }

  post {
    always {
      cleanWs(deleteDirs: true, notFailBuild: true)
    }
  }
}
